from __future__ import annotations

import argparse
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
    args = parser.parse_args()

    try:
        matrix = load_matrix(args.matrix)
        selected = _selected_providers(matrix, args.provider)
        plan = build_plan(matrix, selected, args.scratch_root.resolve(), include_live=args.include_live, dry_run=args.dry_run)
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


def build_plan(matrix: dict[str, Any], providers: list[dict[str, Any]], scratch_root: Path, *, include_live: bool, dry_run: bool = False) -> dict[str, Any]:
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
            }
        )
        if reason:
            commands.append({"provider": provider["name"], "label": "provider_skipped", "status": "skipped", "reason": reason, "command": []})
            continue
        commands.extend(_provider_commands(provider))
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


def _provider_commands(provider: dict[str, Any]) -> list[dict[str, Any]]:
    name = provider["name"]
    supports = provider.get("supports", {})
    commands = [
        _command(name, "provider_check_offline", ["providers", "check", name, "--offline"]),
        _command(name, "provider_check", ["providers", "check", name]),
    ]
    if supports.get("repair", "repair" in provider.get("capabilities", [])):
        commands.append(
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
        commands.append(_command(name, "gen_sva", ["gen-sva", "--spec", "docs/spec.md", "--rtl", "rtl/broken_counter.sv", "--provider", name]))
        commands.append(_command(name, "gen_cocotb", ["gen-cocotb", "--dut", "rtl/broken_counter.sv", "--intent", "counter smoke", "--provider", name]))
    if supports.get("shell", True):
        commands.append(
            {
                **_command(name, "shell_smoke", ["shell", "--plain"]),
                "stdin": f"/providers\n/providers check {name} --offline\n/transcript\n/exit\n",
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
        return {
            "kind": "agent_runtime",
            "runtime": provider.get("runtime", "langgraph"),
            "base_provider": provider["base_provider"],
            "capabilities": ["repair"],
            "max_iterations": int(provider.get("max_iterations", 3)),
            "timeout_seconds": int(provider.get("timeout_seconds", 90)),
        }
    if kind == "local_command":
        command = _env_or_default(provider, "command")
        args = [_template_arg(str(item)) for item in provider.get("args", [])]
        return {
            "kind": "local_command",
            "capabilities": provider.get("capabilities", []),
            "command": command,
            "args": args,
            "timeout_seconds": int(provider.get("timeout_seconds", 30)),
            "output_limit_chars": int(provider.get("output_limit_chars", 65536)),
        }
    if kind == "anthropic":
        return {
            "kind": "anthropic",
            "capabilities": provider.get("capabilities", []),
            "base_url": _env_or_default(provider, "base_url"),
            "endpoint": provider.get("endpoint", "messages"),
            "model": _env_or_default(provider, "model"),
            "api_key_env": provider.get("api_key_env", "ANTHROPIC_API_KEY"),
            "anthropic_version": provider.get("anthropic_version", "2023-06-01"),
            "max_tokens": int(provider.get("max_tokens", 4096)),
            "timeout_seconds": int(provider.get("timeout_seconds", 60)),
        }
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
                "attempt_count": len(parsed.get("attempts", [])) if isinstance(parsed, dict) and isinstance(parsed.get("attempts"), list) else None,
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
        workflow_status = str(parsed.get("status", ""))
        validation_status = _payload_validation_status(parsed)
        patch_id = _payload_patch_id(parsed)
        if workflow_status in {"review_required", "applied"} and validation_status == "passed" and patch_id:
            return "passed", ""
        return "failed", "agent_repair_missing_validated_patch"
    if label in {"gen_sva", "gen_cocotb"}:
        workflow_status = str(parsed.get("status", ""))
        validation_status = _payload_validation_status(parsed)
        candidate_id = _payload_candidate_id(parsed)
        if workflow_status == "no_generation" and command.get("expected") == "no_generation":
            return "passed", ""
        if candidate_id and validation_status == "passed":
            return "passed", ""
        return "failed", f"{label}_missing_validated_candidate"
    return "passed", ""


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
        },
    }


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
        "| Provider | Kind | Status | Reason |",
        "| --- | --- | --- | --- |",
    ]
    for provider in summary["providers"]:
        lines.append(f"| `{provider['name']}` | `{provider['kind']}` | `{provider['status']}` | `{provider.get('reason') or ''}` |")
    lines.extend(["", "## Results", "", "| Provider | Scenario | Status | Exit | Seconds | Validation | Candidate | Attempts |"])
    lines.append("| --- | --- | --- | ---: | ---: | --- | --- | ---: |")
    for item in summary["results"]:
        lines.append(
            "| "
            f"`{item.get('provider', '')}` | "
            f"`{item.get('label', '')}` | "
            f"`{item.get('status', '')}` | "
            f"{item.get('exit_code', '')} | "
            f"{item.get('elapsed_seconds', '')} | "
            f"{item.get('validation_status') or ''} | "
            f"{item.get('candidate_id') or ''} | "
            f"{item.get('attempt_count') or ''} |"
        )
    lines.extend(["", "Stdout/stderr are bounded in the JSON summary. Secret-looking environment values are redacted."])
    return "\n".join(lines) + "\n"


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value).strip("-") or "matrix"


if __name__ == "__main__":
    raise SystemExit(main())
