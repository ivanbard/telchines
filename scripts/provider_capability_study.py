from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCRATCH_ROOT = REPO_ROOT / ".test-work" / "provider-capability-study"
SUPPORTED_KINDS = {"openai_compatible", "anthropic", "local_command", "agent_runtime"}
SECRET_KEY_PARTS = ("api_key", "apikey", "authorization", "bearer", "secret", "token", "password")


class MatrixError(ValueError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a no-secret Telchines LLM provider capability study.")
    parser.add_argument("--matrix", type=Path, required=True, help="Provider capability matrix JSON file.")
    parser.add_argument("--provider", help="Optional provider name filter.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and list commands without running providers.")
    parser.add_argument("--scratch-root", type=Path, default=DEFAULT_SCRATCH_ROOT)
    parser.add_argument("--include-live", action="store_true", help="Allow live HTTP/local-server providers when their env gates are set.")
    parser.add_argument("--max-live-commands", type=int, help="Optional cap on non-dry-run commands executed.")
    parser.add_argument("--repeat-count", type=int, default=1, help="Repeat each planned provider scenario this many times.")
    args = parser.parse_args()

    try:
        matrix = load_matrix(args.matrix)
        selected = _selected_providers(matrix, args.provider)
        plan = build_plan(matrix, selected, args.scratch_root.resolve(), include_live=args.include_live, dry_run=args.dry_run, repeat_count=args.repeat_count)
    except MatrixError as exc:
        print(json.dumps({"status": "invalid_matrix", "error": str(exc)}, indent=2))
        return 2

    if args.dry_run:
        print(json.dumps({"status": "dry_run", "matrix": matrix["name"], "providers": plan["providers"], "commands": plan["commands"]}, indent=2))
        return 0

    if args.max_live_commands is not None:
        _apply_command_budget(plan["commands"], args.max_live_commands)

    scratch = Path(plan["scratch_root"])
    active_providers = _active_providers(selected, plan["commands"])
    _prepare_scratch_project(scratch, matrix, active_providers)
    results = _run_commands(scratch, plan["commands"])
    summary = _summary(matrix, scratch, plan, results)
    summary = _redact_summary(summary)
    results_path = scratch / f"{matrix['name']}_provider_capability_summary.json"
    report_path = scratch / f"{matrix['name']}_provider_capability_summary.md"
    results_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    report_path.write_text(_markdown_report(summary), encoding="utf-8")
    print(json.dumps({"status": summary["status"], "summary_path": str(results_path), "report_path": str(report_path)}, indent=2))
    return 0 if summary["status"] in {"passed", "skipped"} else 1


def load_matrix(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    _validate_matrix(payload, path)
    return payload


def build_plan(
    matrix: dict[str, Any],
    providers: list[dict[str, Any]],
    scratch_root: Path,
    *,
    include_live: bool,
    dry_run: bool = False,
    repeat_count: int = 1,
) -> dict[str, Any]:
    if repeat_count < 1:
        raise MatrixError("repeat-count must be at least 1")
    commands: list[dict[str, Any]] = []
    provider_summaries: list[dict[str, Any]] = []
    skip_reasons: dict[str, str] = {}
    for provider in providers:
        skip_reasons[provider["name"]] = "" if dry_run else _provider_skip_reason(provider, include_live)
    for provider in providers:
        if provider.get("kind") == "agent_runtime":
            base_provider = str(provider.get("base_provider"))
            base_reason = skip_reasons.get(base_provider, "")
            if base_reason:
                skip_reasons[provider["name"]] = f"base_provider_skipped:{base_provider}:{base_reason}"
    for provider in providers:
        reason = skip_reasons[provider["name"]]
        provider_summaries.append(
            {
                "name": provider["name"],
                "kind": provider["kind"],
                "capabilities": provider.get("capabilities", []),
                "status": "skipped" if reason else "planned",
                "reason": reason,
                **_provider_model_fields(provider),
            }
        )
        if reason:
            commands.append(
                {
                    "provider": provider["name"],
                    "label": "provider_skipped",
                    "status": "skipped",
                    "reason": reason,
                    "command": [],
                    **_provider_model_fields(provider),
                }
            )
            continue
        commands.extend(_provider_commands(provider, repeat_count=repeat_count))
    return {
        "matrix": matrix["name"],
        "scratch_root": str(scratch_root / _safe_name(matrix["name"])),
        "providers": provider_summaries,
        "commands": commands,
    }


def _validate_matrix(payload: dict[str, Any], path: Path) -> None:
    if not isinstance(payload, dict):
        raise MatrixError(f"{path} must contain a JSON object")
    if payload.get("schema_version") != "0.1":
        raise MatrixError("schema_version must be 0.1")
    if not isinstance(payload.get("name"), str) or not payload["name"].strip():
        raise MatrixError("matrix.name must be a non-empty string")
    providers = payload.get("providers")
    if not isinstance(providers, list) or not providers:
        raise MatrixError("matrix.providers must be a non-empty list")
    seen: set[str] = set()
    for provider in providers:
        if not isinstance(provider, dict):
            raise MatrixError("provider entries must be objects")
        name = provider.get("name")
        if not isinstance(name, str) or not name.strip():
            raise MatrixError("provider.name must be a non-empty string")
        if name in seen:
            raise MatrixError(f"duplicate provider name: {name}")
        seen.add(name)
        kind = provider.get("kind")
        if kind not in SUPPORTED_KINDS:
            raise MatrixError(f"provider {name} has unsupported kind: {kind}")
        capabilities = provider.get("capabilities", [])
        if not isinstance(capabilities, list) or any(item not in {"repair", "generation"} for item in capabilities):
            raise MatrixError(f"provider {name} capabilities must be repair and/or generation")
        if kind in {"openai_compatible", "anthropic", "local_command"} and not capabilities:
            raise MatrixError(f"provider {name} must declare at least one capability")
        if kind == "agent_runtime":
            if capabilities != ["repair"]:
                raise MatrixError(f"agent_runtime provider {name} must declare capabilities ['repair']")
            base_provider = provider.get("base_provider")
            if not isinstance(base_provider, str) or not base_provider.strip():
                raise MatrixError(f"agent_runtime provider {name} must define base_provider")
            if base_provider not in seen and not any(item.get("name") == base_provider for item in providers if isinstance(item, dict)):
                raise MatrixError(f"agent_runtime provider {name} references unknown base_provider {base_provider}")
        _reject_literal_secrets(provider, f"provider {name}")


def _reject_literal_secrets(value: Any, path: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in SECRET_KEY_PARTS) and lowered not in {"api_key_env", "enabled_env", "model_env", "base_url_env", "max_tokens"}:
                raise MatrixError(f"{path}.{key} looks like a literal secret field; use an *_env field instead")
            _reject_literal_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_literal_secrets(item, f"{path}[{index}]")


def _selected_providers(matrix: dict[str, Any], provider_name: str | None) -> list[dict[str, Any]]:
    providers = matrix["providers"]
    if provider_name is None:
        return providers
    selected = [provider for provider in providers if provider["name"] == provider_name]
    if not selected:
        raise MatrixError(f"provider filter did not match matrix provider: {provider_name}")
    by_name = {provider["name"]: provider for provider in providers}
    expanded = list(selected)
    for provider in selected:
        base_provider = provider.get("base_provider")
        if isinstance(base_provider, str) and base_provider in by_name and by_name[base_provider] not in expanded:
            expanded.insert(0, by_name[base_provider])
    return expanded


def _provider_skip_reason(provider: dict[str, Any], include_live: bool) -> str:
    if provider.get("live", False):
        if not include_live:
            return "disabled_live"
        enabled_env = provider.get("enabled_env")
        if isinstance(enabled_env, str) and enabled_env and os.environ.get(enabled_env) != "1":
            return f"missing_env:{enabled_env}"
    for env_key in ("api_key_env", "model_env", "base_url_env", "command_env"):
        env_name = provider.get(env_key)
        if isinstance(env_name, str) and env_name and not provider.get(env_key.replace("_env", "_default")):
            if not os.environ.get(env_name):
                return f"missing_env:{env_name}"
    return ""


def _provider_commands(provider: dict[str, Any], *, repeat_count: int = 1) -> list[dict[str, Any]]:
    name = provider["name"]
    supports = provider.get("supports", {})
    base_commands = [
        _command(name, "provider_check_offline", ["providers", "check", name, "--offline"]),
        _command(name, "provider_check", ["providers", "check", name]),
    ]
    if supports.get("repair", "repair" in provider.get("capabilities", [])):
        base_commands.append(
            _command(
                name,
                "agent_repair",
                [
                    "agent",
                    "Fix the broken counter compile failure and leave the validated RTL patch ready for review",
                    "--tool",
                    "fixture",
                    "--file",
                    "rtl/broken_counter.sv",
                    "--provider",
                    name,
                ],
            )
        )
    if supports.get("generation", "generation" in provider.get("capabilities", [])):
        base_commands.append(_command(name, "gen_sva", ["gen-sva", "--spec", "docs/spec.md", "--rtl", "rtl/broken_counter.sv", "--provider", name]))
        base_commands.append(_command(name, "gen_cocotb", ["gen-cocotb", "--dut", "rtl/broken_counter.sv", "--intent", "counter smoke", "--provider", name]))
    if supports.get("shell", True):
        base_commands.append(
            {
                **_command(name, "shell_smoke", ["shell", "--plain"]),
                "stdin": f"/providers\n/model list --offline\n/providers check {name} --offline\n/transcript\n/exit\n",
            }
        )
    commands: list[dict[str, Any]] = []
    provider_fields = _provider_model_fields(provider)
    for command in base_commands:
        for repeat_index in range(1, repeat_count + 1):
            commands.append(
                {
                    **command,
                    **provider_fields,
                    "scenario_id": f"{name}:{command['label']}",
                    "repeat_index": repeat_index,
                    "repeat_count": repeat_count,
                }
            )
    return commands


def _active_providers(providers: list[dict[str, Any]], commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active_names = {str(command.get("provider")) for command in commands if command.get("status") == "planned"}
    return [provider for provider in providers if provider["name"] in active_names]


def _apply_command_budget(commands: list[dict[str, Any]], max_commands: int) -> None:
    max_commands = max(max_commands, 0)
    executed = 0
    for command in commands:
        if command.get("status") != "planned":
            continue
        if executed < max_commands:
            executed += 1
            continue
        command["status"] = "skipped"
        command["reason"] = "max_live_commands"


def _command(provider: str, label: str, args: list[str]) -> dict[str, Any]:
    return {"provider": provider, "label": label, "status": "planned", "command": [sys.executable, "-m", "telchines", *args]}


def _provider_model_fields(provider: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider_kind": provider.get("kind"),
        "model": provider.get("model") or provider.get("model_default"),
        "reasoning_level": provider.get("reasoning_level", "auto"),
        "reasoning_summary": provider.get("reasoning_summary"),
        "reasoning_wire_format": provider.get("reasoning_wire_format"),
        "model_source": provider.get("model_source") or ("configured" if provider.get("model") or provider.get("model_default") else "preset"),
        "supports_reasoning_effort": provider.get("supports_reasoning_effort"),
        "model_warnings": provider.get("model_warnings", []),
    }


def _prepare_scratch_project(scratch: Path, matrix: dict[str, Any], providers: list[dict[str, Any]]) -> None:
    if scratch.exists():
        shutil.rmtree(scratch)
    fixture = REPO_ROOT / "benchmarks" / "assets" / "repair_missing_semicolon"
    shutil.copytree(fixture, scratch)
    _ensure_generation_fixture_files(scratch)
    _write_local_fixture_provider(scratch)
    subprocess.run(
        [sys.executable, "-m", "telchines", "project", "init", ".", "--name", f"{matrix['name']}-provider-study"],
        cwd=scratch,
        env=_subprocess_env(),
        check=True,
    )
    config_path = scratch / ".tel" / "config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    provider_configs = {provider["name"]: _provider_config(provider) for provider in providers}
    default_repair = _first_provider_with_capability(providers, "repair") or "heuristic"
    default_generation = _first_provider_with_capability(providers, "generation") or "heuristic"
    provider_configs.setdefault("heuristic", {"kind": "heuristic", "capabilities": ["repair", "generation"]})
    payload["model_mode"] = "hybrid"
    payload["adapters"] = ["fixture", "verilator", "slang", "symbiyosys"]
    payload["generation"]["sva"]["max_attempts"] = 2
    payload["generation"]["cocotb"]["max_attempts"] = 2
    payload["project"]["model_policy"] = {
        "default_provider_by_capability": {"repair": default_repair, "generation": default_generation},
        "providers": provider_configs,
    }
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _provider_config(provider: dict[str, Any]) -> dict[str, Any]:
    kind = provider["kind"]
    if kind == "agent_runtime":
        return _with_model_selection(
            provider,
            {
            "kind": "agent_runtime",
            "runtime": provider.get("runtime", "langgraph"),
            "base_provider": provider["base_provider"],
            "capabilities": ["repair"],
            "max_iterations": int(provider.get("max_iterations", 3)),
            "timeout_seconds": int(provider.get("timeout_seconds", 90)),
            },
        )
    if kind == "local_command":
        command = _env_or_default(provider, "command")
        args = [_template_arg(str(item)) for item in provider.get("args", [])]
        return _with_model_selection(
            provider,
            {
                "kind": "local_command",
                "capabilities": provider.get("capabilities", []),
                "command": command,
                "args": args,
                "timeout_seconds": int(provider.get("timeout_seconds", 30)),
                "output_limit_chars": int(provider.get("output_limit_chars", 65536)),
            },
        )
    if kind == "anthropic":
        return _with_model_selection(
            provider,
            {
                "kind": "anthropic",
                "capabilities": provider.get("capabilities", []),
                "base_url": _env_or_default(provider, "base_url"),
                "endpoint": provider.get("endpoint", "messages"),
                "model": _env_or_default(provider, "model"),
                "api_key_env": provider.get("api_key_env", "ANTHROPIC_API_KEY"),
                "anthropic_version": provider.get("anthropic_version", "2023-06-01"),
                "max_tokens": int(provider.get("max_tokens", 4096)),
                "timeout_seconds": int(provider.get("timeout_seconds", 60)),
            },
        )
    config = {
        "kind": "openai_compatible",
        "capabilities": provider.get("capabilities", []),
        "base_url": _env_or_default(provider, "base_url"),
        "endpoint": provider.get("endpoint", "chat/completions"),
        "model": _env_or_default(provider, "model"),
        "api_key_env": provider.get("api_key_env", "OPENAI_API_KEY"),
        "timeout_seconds": int(provider.get("timeout_seconds", 60)),
    }
    if provider.get("auth") == "none":
        config["auth"] = "none"
    if provider.get("supports_reasoning_effort") is not None:
        config["supports_reasoning_effort"] = bool(provider.get("supports_reasoning_effort"))
    return _with_model_selection(provider, config)


def _with_model_selection(provider: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    for field in ("model_source", "reasoning_level", "reasoning_summary", "reasoning_wire_format"):
        if provider.get(field) is not None:
            config[field] = provider[field]
    return config


def _env_or_default(provider: dict[str, Any], field: str) -> str:
    env_name = provider.get(f"{field}_env")
    if isinstance(env_name, str) and env_name and os.environ.get(env_name):
        return os.environ[env_name]
    value = provider.get(field)
    if isinstance(value, str) and value:
        return _template_arg(value)
    default = provider.get(f"{field}_default")
    if isinstance(default, str) and default:
        return _template_arg(default)
    raise MatrixError(f"provider {provider['name']} is missing {field} or {field}_env")


def _template_arg(value: str) -> str:
    return value.replace("{python}", sys.executable)


def _first_provider_with_capability(providers: list[dict[str, Any]], capability: str) -> str | None:
    for provider in providers:
        if capability in provider.get("capabilities", []):
            return provider["name"]
    return None


def _ensure_generation_fixture_files(scratch: Path) -> None:
    docs = scratch / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "spec.md").write_text(
        """# Broken Counter Spec

The `broken_counter` module has an active-low reset. When `rst_n` is low, `count` resets to zero. On each rising edge after reset, `count` increments by one.
""",
        encoding="utf-8",
    )


def _write_local_fixture_provider(scratch: Path) -> None:
    tools = scratch / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "matrix_local_provider.py").write_text(
        r'''from __future__ import annotations
import json
import sys
from pathlib import Path

payload = json.loads(sys.stdin.read())
workflow = payload.get("workflow_type")

if workflow == "provider_check":
    print("local fixture provider log")
    print(json.dumps({"status": "ok", "workflow_type": workflow}))
    raise SystemExit(0)

if workflow == "compile_repair":
    target_file = payload["files"][0]
    original = Path(target_file).read_text(encoding="utf-8")
    fixed = original.replace("count <= 4'd0", "count <= 4'd0;")
    print(json.dumps({
        "status": "proposed",
        "file_path": target_file,
        "candidate_content": fixed,
        "explanation": "Local fixture provider added the missing reset semicolon.",
        "evidence_paths": ["docs/spec.md"],
    }))
    raise SystemExit(0)

if workflow == "spec_to_sva":
    invalid = not payload.get("previous_attempts")
    content = """module broken_counter_assertions(
  input logic clk,
  input logic rst_n,
  input logic [3:0] count
);

property p_reset_clears_count;
  @(posedge clk) !rst_n |=> count == 4'd0;
endproperty

assert property (p_reset_clears_count);

endmodule

bind broken_counter broken_counter_assertions broken_counter_assertions_i(
  .clk(clk),
  .rst_n(rst_n),
  .count(count)
);
"""
    if invalid:
        content = "module broken_counter_assertions;\nproperty p_missing;\nassert property (p_missing);\n"
    print(json.dumps({
        "status": "proposed",
        "file_path": payload["output_file"],
        "candidate_content": content,
        "explanation": "Local fixture provider generated reset assertion coverage.",
        "evidence_paths": [payload["spec"]["path"], payload["rtl"]["path"]],
        "properties": [{"name": "p_reset_clears_count", "summary": "Reset clears count.", "rationale": "Grounded in spec.", "source_citation": payload["spec"]["path"]}],
    }))
    raise SystemExit(0)

if workflow == "dut_to_cocotb":
    invalid = not payload.get("previous_attempts")
    content = """import cocotb

@cocotb.test()
async def test_counter_smoke(dut):
    dut._log.info("counter smoke")
"""
    if invalid:
        content = """import cocotb

@cocotb.test()
async def test_counter_smoke(dut):
    dut._log.info("unterminated)
"""
    print(json.dumps({
        "status": "proposed",
        "file_path": payload["default_output_file"],
        "manifest_path": payload["default_manifest_file"],
        "candidate_content": content,
        "top_module": payload["dut"]["module_name"],
        "explanation": "Local fixture provider generated cocotb smoke.",
        "assumptions": payload["inference"]["assumptions"],
        "ports": payload["dut"]["ports"],
        "evidence_paths": [payload["dut"]["path"]],
    }))
    raise SystemExit(0)

print(json.dumps({"status": "no_generation", "summary": f"unsupported workflow {workflow}"}))
''',
        encoding="utf-8",
    )


def _run_commands(scratch: Path, commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in commands:
        if item["status"] != "planned":
            results.append(dict(item))
            continue
        started = time.perf_counter()
        process = subprocess.run(
            item["command"],
            cwd=scratch,
            input=item.get("stdin"),
            capture_output=True,
            text=True,
            env=_subprocess_env(),
            check=False,
        )
        elapsed = round(time.perf_counter() - started, 3)
        parsed = _parse_last_json(process.stdout)
        status, status_reason = _score_command_result(item, process.returncode, parsed)
        validation_status = _payload_validation_status(parsed)
        results.append(
            {
                **item,
                "exit_code": process.returncode,
                "elapsed_seconds": elapsed,
                "stdout": _bounded(process.stdout),
                "stderr": _bounded(process.stderr),
                "parsed": parsed,
                "status": status,
                "status_reason": status_reason,
                "validation_status": validation_status,
                "candidate_id": _payload_candidate_id(parsed),
                "patch_id": _payload_patch_id(parsed),
                "attempt_count": len(_payload_attempts(parsed)) if _payload_attempts(parsed) else None,
                "retry_count": _payload_retry_count(parsed),
                "json_repair_attempt_count": _payload_json_repair_attempt_count(parsed),
                "validation_delta": _payload_validation_delta(parsed),
                "semantic_fingerprint": _semantic_fingerprint(item, parsed),
            }
        )
    return results


def _score_command_result(command: dict[str, Any], returncode: int, parsed: dict[str, Any]) -> tuple[str, str]:
    label = str(command.get("label", ""))
    if returncode != 0:
        return "failed", f"process_exit:{returncode}"
    if label in {"provider_check", "provider_check_offline"}:
        return ("passed", "") if parsed.get("status") == "passed" else ("failed", "provider_check_status_not_passed")
    if label == "agent_repair":
        no_op_reason = _unexpected_workflow_no_op(parsed, expected=str(command.get("expected") or ""))
        if no_op_reason:
            return "failed", no_op_reason
        workflow_status = str(parsed.get("status", ""))
        validation_status = _payload_validation_status(parsed)
        patch_id = _payload_patch_id(parsed)
        if workflow_status in {"review_required", "applied"} and validation_status == "passed" and patch_id:
            return "passed", ""
        return "failed", "agent_repair_missing_validated_patch"
    if label in {"gen_sva", "gen_cocotb"}:
        no_op_reason = _unexpected_workflow_no_op(parsed, expected=str(command.get("expected") or ""))
        if no_op_reason:
            return "failed", no_op_reason
        workflow_status = str(parsed.get("status", ""))
        validation_status = _payload_validation_status(parsed)
        candidate_id = _payload_candidate_id(parsed)
        if workflow_status == "no_generation" and command.get("expected") == "no_generation":
            return "passed", ""
        if candidate_id and validation_status == "passed":
            return "passed", ""
        return "failed", f"{label}_missing_validated_candidate"
    return "passed", ""


def _unexpected_workflow_no_op(parsed: dict[str, Any], *, expected: str = "") -> str:
    expected_statuses = {item.strip() for item in expected.split(",") if item.strip()}
    bad_statuses = {"failed", "no_patch", "no_generation", "rejected"}
    for location, status in _payload_workflow_statuses(parsed):
        if status in bad_statuses and status not in expected_statuses:
            return f"workflow_{location}:{status}"
    return ""


def _payload_workflow_statuses(parsed: dict[str, Any]) -> list[tuple[str, str]]:
    if not isinstance(parsed, dict):
        return []
    statuses: list[tuple[str, str]] = []
    for key in ("status", "workflow_status", "candidate_status"):
        value = parsed.get(key)
        if isinstance(value, str) and value:
            statuses.append((key, value))
    result = parsed.get("result")
    if isinstance(result, dict):
        for key in ("status", "workflow_status", "candidate_status"):
            value = result.get(key)
            if isinstance(value, str) and value:
                statuses.append((f"result.{key}", value))
    return statuses


def _payload_validation_status(parsed: dict[str, Any]) -> Any:
    if not isinstance(parsed, dict):
        return None
    if parsed.get("validation_status") is not None:
        return parsed.get("validation_status")
    result = parsed.get("result")
    if isinstance(result, dict):
        return result.get("validation_status")
    return None


def _payload_candidate_id(parsed: dict[str, Any]) -> Any:
    if not isinstance(parsed, dict):
        return None
    if parsed.get("candidate_id"):
        return parsed.get("candidate_id")
    result = parsed.get("result")
    if isinstance(result, dict):
        return result.get("candidate_id")
    return None


def _payload_patch_id(parsed: dict[str, Any]) -> Any:
    if not isinstance(parsed, dict):
        return None
    if parsed.get("patch_id"):
        return parsed.get("patch_id")
    result = parsed.get("result")
    if isinstance(result, dict) and result.get("patch_id"):
        return result.get("patch_id")
    evidence = parsed.get("evidence")
    if isinstance(evidence, dict):
        return evidence.get("patch_id")
    return None


def _payload_attempts(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(parsed, dict):
        return []
    attempts = parsed.get("attempts")
    if isinstance(attempts, list):
        return [item for item in attempts if isinstance(item, dict)]
    result = parsed.get("result")
    if isinstance(result, dict) and isinstance(result.get("attempts"), list):
        return [item for item in result["attempts"] if isinstance(item, dict)]
    return []


def _payload_retry_count(parsed: dict[str, Any]) -> int | None:
    attempts = _payload_attempts(parsed)
    if not attempts:
        return None
    return max(len(attempts) - 1, 0)


def _payload_json_repair_attempt_count(parsed: dict[str, Any]) -> int:
    if not isinstance(parsed, dict):
        return 0
    for key in ("json_repair_attempt_count", "json_repair_attempts"):
        value = parsed.get(key)
        if isinstance(value, int):
            return max(value, 0)
        if isinstance(value, list):
            return len(value)
    result = parsed.get("result")
    if isinstance(result, dict):
        for key in ("json_repair_attempt_count", "json_repair_attempts"):
            value = result.get(key)
            if isinstance(value, int):
                return max(value, 0)
            if isinstance(value, list):
                return len(value)
    return 0


def _payload_validation_delta(parsed: dict[str, Any]) -> dict[str, Any]:
    attempts = _payload_attempts(parsed)
    validation_statuses = [str(item.get("validation_status")) for item in attempts if item.get("validation_status") is not None]
    candidate_statuses = [str(item.get("status")) for item in attempts if item.get("status") is not None]
    rejected_candidate_ids = parsed.get("rejected_candidate_ids") if isinstance(parsed, dict) else None
    result = parsed.get("result") if isinstance(parsed, dict) else None
    if not isinstance(rejected_candidate_ids, list) and isinstance(result, dict):
        rejected_candidate_ids = result.get("rejected_candidate_ids")
    return {
        "attempt_count": len(attempts),
        "first_validation_status": validation_statuses[0] if validation_statuses else None,
        "final_validation_status": validation_statuses[-1] if validation_statuses else _payload_validation_status(parsed),
        "candidate_statuses": candidate_statuses,
        "rejected_candidate_count": len(rejected_candidate_ids) if isinstance(rejected_candidate_ids, list) else 0,
    }


def _semantic_fingerprint(command: dict[str, Any], parsed: dict[str, Any]) -> str | None:
    if not isinstance(parsed, dict):
        return None
    label = str(command.get("label", ""))
    material = {
        "label": label,
        "status": parsed.get("status"),
        "validation_status": _payload_validation_status(parsed),
        "has_candidate": bool(_payload_candidate_id(parsed)),
        "has_patch": bool(_payload_patch_id(parsed)),
        "artifact_path": parsed.get("artifact_path") or parsed.get("file_path") or parsed.get("path"),
        "validation_delta": _payload_validation_delta(parsed),
    }
    result = parsed.get("result")
    if isinstance(result, dict):
        material["result_status"] = result.get("status")
        material["file_path"] = result.get("artifact_path") or result.get("file_path") or result.get("path")
    if label in {"provider_check", "provider_check_offline"}:
        material["provider_check_status"] = parsed.get("status")
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _parse_last_json(stdout: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    parsed: dict[str, Any] = {}
    parsed_length = -1
    for index, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and end > parsed_length:
            parsed = value
            parsed_length = end
    return parsed


def _summary(matrix: dict[str, Any], scratch: Path, plan: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [item for item in results if item.get("status") == "failed"]
    passed = [item for item in results if item.get("status") == "passed"]
    skipped = [item for item in results if item.get("status") == "skipped"]
    status = "failed" if failed else ("skipped" if skipped and not passed else "passed")
    return {
        "schema_version": "0.1",
        "matrix": matrix["name"],
        "status": status,
        "scratch_root": str(scratch),
        "providers": plan["providers"],
        "results": results,
        "metrics": {
            "passed": len(passed),
            "failed": len(failed),
            "skipped": len(skipped),
            "total": len(results),
            "stability": _stability_metrics(results),
        },
    }


def _stability_metrics(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for item in results:
        if item.get("status") == "skipped":
            continue
        key = (
            str(item.get("provider", "")),
            str(item.get("label", "")),
            str(item.get("model") or ""),
            str(item.get("reasoning_level") or "auto"),
        )
        groups.setdefault(key, []).append(item)
    metrics: list[dict[str, Any]] = []
    for (provider, label, model, reasoning_level), items in sorted(groups.items()):
        fingerprints = sorted({str(item.get("semantic_fingerprint")) for item in items if item.get("semantic_fingerprint")})
        statuses = sorted({str(item.get("status")) for item in items})
        latencies = [float(item.get("elapsed_seconds", 0)) for item in items if isinstance(item.get("elapsed_seconds"), (int, float))]
        retry_counts = [int(item.get("retry_count", 0)) for item in items if isinstance(item.get("retry_count"), int)]
        json_repair_counts = [
            int(item.get("json_repair_attempt_count", 0)) for item in items if isinstance(item.get("json_repair_attempt_count"), int)
        ]
        validation_deltas = [item.get("validation_delta") for item in items if isinstance(item.get("validation_delta"), dict)]
        final_validation_statuses = sorted(
            {
                str(delta.get("final_validation_status"))
                for delta in validation_deltas
                if delta.get("final_validation_status") is not None
            }
        )
        metrics.append(
            {
                "provider": provider,
                "label": label,
                "model": model or None,
                "reasoning_level": reasoning_level,
                "runs": len(items),
                "statuses": statuses,
                "fingerprints": fingerprints,
                "stable": len(statuses) == 1 and len(fingerprints) <= 1,
                "latency_seconds_min": min(latencies) if latencies else None,
                "latency_seconds_max": max(latencies) if latencies else None,
                "latency_seconds_avg": round(sum(latencies) / len(latencies), 3) if latencies else None,
                "retry_count_min": min(retry_counts) if retry_counts else None,
                "retry_count_max": max(retry_counts) if retry_counts else None,
                "json_repair_attempt_count_max": max(json_repair_counts) if json_repair_counts else 0,
                "validation_final_statuses": final_validation_statuses,
                "validation_delta_stable": len(final_validation_statuses) <= 1,
            }
        )
    return metrics


def _redact_summary(value: Any) -> Any:
    secrets = [secret for key, secret in os.environ.items() if _looks_secret_key(key) and secret]
    text = json.dumps(value)
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    return json.loads(text)


def _looks_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SECRET_KEY_PARTS)


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(REPO_ROOT / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src_path if not existing else os.pathsep.join([src_path, existing])
    return env


def _bounded(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n... truncated {len(value) - limit} chars"


def _markdown_report(summary: dict[str, Any]) -> str:
    lines = [
        f"# Provider Capability Study: {summary['matrix']}",
        "",
        f"Status: `{summary['status']}`",
        f"Scratch root: `{summary['scratch_root']}`",
        "",
        "## Providers",
        "",
        "| Provider | Kind | Model | Reasoning | Status | Reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for provider in summary["providers"]:
        lines.append(
            f"| `{provider['name']}` | `{provider['kind']}` | "
            f"`{provider.get('model') or ''}` | `{provider.get('reasoning_level') or 'auto'}` | "
            f"`{provider['status']}` | `{provider.get('reason') or ''}` |"
        )
    lines.extend(["", "## Results", "", "| Provider | Scenario | Repeat | Model | Reasoning | Status | Exit | Seconds | Validation | Candidate | Attempts | Retries | JSON repair | Fingerprint |"])
    lines.append("| --- | --- | ---: | --- | --- | --- | ---: | ---: | --- | --- | ---: | ---: | ---: | --- |")
    for item in summary["results"]:
        lines.append(
            "| "
            f"`{item.get('provider', '')}` | "
            f"`{item.get('label', '')}` | "
            f"{item.get('repeat_index') or ''} | "
            f"`{item.get('model') or ''}` | "
            f"`{item.get('reasoning_level') or 'auto'}` | "
            f"`{item.get('status', '')}` | "
            f"{item.get('exit_code', '')} | "
            f"{item.get('elapsed_seconds', '')} | "
            f"{item.get('validation_status') or ''} | "
            f"{item.get('candidate_id') or ''} | "
            f"{item.get('attempt_count') or ''} | "
            f"{item.get('retry_count') if item.get('retry_count') is not None else ''} | "
            f"{item.get('json_repair_attempt_count') if item.get('json_repair_attempt_count') is not None else ''} | "
            f"`{item.get('semantic_fingerprint') or ''}` |"
        )
    lines.extend(["", "## Stability", "", "| Provider | Scenario | Model | Reasoning | Runs | Stable | Statuses | Validation | Retry range | JSON repair max | Fingerprints |"])
    lines.append("| --- | --- | --- | --- | ---: | --- | --- | --- | --- | ---: | --- |")
    for item in summary.get("metrics", {}).get("stability", []):
        retry_range = ""
        if item.get("retry_count_min") is not None and item.get("retry_count_max") is not None:
            retry_range = f"{item.get('retry_count_min')}..{item.get('retry_count_max')}"
        lines.append(
            "| "
            f"`{item.get('provider', '')}` | "
            f"`{item.get('label', '')}` | "
            f"`{item.get('model') or ''}` | "
            f"`{item.get('reasoning_level') or 'auto'}` | "
            f"{item.get('runs', '')} | "
            f"{item.get('stable', '')} | "
            f"`{','.join(item.get('statuses', []))}` | "
            f"`{','.join(item.get('validation_final_statuses', []))}` | "
            f"`{retry_range}` | "
            f"{item.get('json_repair_attempt_count_max', 0)} | "
            f"`{','.join(item.get('fingerprints', []))}` |"
        )
    lines.extend(["", "Stdout/stderr are bounded in the JSON summary. Secret-looking environment values are redacted."])
    return "\n".join(lines) + "\n"


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value).strip("-") or "matrix"


if __name__ == "__main__":
    raise SystemExit(main())
