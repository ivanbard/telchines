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
SCRATCH_ROOT = REPO_ROOT / ".test-work" / "openrouter-capability-study"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a no-secret OpenRouter capability study in a scratch Telchines project.")
    parser.add_argument("--dry-run", action="store_true", help="List the study matrix without calling live providers.")
    parser.add_argument("--scratch-root", type=Path, default=SCRATCH_ROOT)
    parser.add_argument("--free-model", default="cohere/north-mini-code:free")
    parser.add_argument("--qwen-model", default="qwen/qwen3.7-plus")
    parser.add_argument("--max-model", default="qwen/qwen3.7-max")
    args = parser.parse_args()

    commands = _study_commands()
    if args.dry_run:
        print(json.dumps({"status": "dry_run", "commands": commands}, indent=2))
        return 0

    if not os.environ.get("OPENROUTER_API_KEY"):
        print(json.dumps({"status": "skipped_missing_key", "missing_env": "OPENROUTER_API_KEY"}, indent=2))
        return 0

    scratch = args.scratch_root.resolve()
    if scratch.exists():
        shutil.rmtree(scratch)
    fixture = REPO_ROOT / "benchmarks" / "assets" / "repair_missing_semicolon"
    shutil.copytree(fixture, scratch)
    _ensure_generation_fixture_files(scratch)
    _ensure_project_config(scratch, args)

    results: list[dict[str, Any]] = []
    for label, command in commands:
        started = time.perf_counter()
        process = subprocess.run(command, cwd=scratch, capture_output=True, text=True, check=False)
        elapsed = round(time.perf_counter() - started, 3)
        results.append(
            {
                "label": label,
                "command": command,
                "exit_code": process.returncode,
                "elapsed_seconds": elapsed,
                "stdout": _bounded(process.stdout),
                "stderr": _bounded(process.stderr),
                "status": "passed" if process.returncode == 0 else "failed",
            }
        )

    summary = {
        "status": "passed" if all(item["exit_code"] == 0 for item in results) else "failed",
        "scratch_root": str(scratch),
        "models": {
            "openrouter-free": args.free_model,
            "openrouter-qwen": args.qwen_model,
            "openrouter-qwen-max": args.max_model,
        },
        "results": results,
    }
    results_path = scratch / "openrouter_capability_summary.json"
    report_path = scratch / "openrouter_capability_summary.md"
    results_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    report_path.write_text(_markdown_report(summary), encoding="utf-8")
    print(json.dumps({"status": summary["status"], "summary_path": str(results_path), "report_path": str(report_path)}, indent=2))
    return 0 if summary["status"] == "passed" else 1


def _study_commands() -> list[tuple[str, list[str]]]:
    python = sys.executable
    return [
        ("provider_free", [python, "-m", "telchines", "providers", "check", "openrouter-free"]),
        ("provider_qwen", [python, "-m", "telchines", "providers", "check", "openrouter-qwen"]),
        ("provider_agent", [python, "-m", "telchines", "providers", "check", "openrouter-agent-qwen"]),
        (
            "agent_repair",
            [
                python,
                "-m",
                "telchines",
                "agent",
                "Fix the broken counter compile failure and leave the validated RTL patch ready for review",
                "--tool",
                "fixture",
                "--file",
                "rtl/broken_counter.sv",
            ],
        ),
        ("gen_sva", [python, "-m", "telchines", "gen-sva", "--spec", "docs/spec.md", "--rtl", "rtl/broken_counter.sv"]),
        ("gen_cocotb", [python, "-m", "telchines", "gen-cocotb", "--dut", "rtl/broken_counter.sv", "--intent", "counter smoke"]),
        (
            "shell_smoke",
            [
                python,
                "-m",
                "telchines",
                "shell",
                "--plain",
            ],
        ),
    ]


def _ensure_project_config(scratch: Path, args: argparse.Namespace) -> None:
    subprocess.run([sys.executable, "-m", "telchines", "project", "init", ".", "--name", "openrouter-capability-study"], cwd=scratch, check=True)
    config_path = scratch / ".tel" / "config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["model_mode"] = "hybrid"
    payload["adapters"] = ["fixture", "verilator", "slang", "symbiyosys"]
    payload["generation"]["sva"]["max_attempts"] = 2
    payload["generation"]["cocotb"]["max_attempts"] = 2
    payload["project"]["model_policy"] = {
        "default_provider_by_capability": {
            "repair": "openrouter-agent-qwen",
            "generation": "openrouter-qwen",
        },
        "providers": {
            "openrouter-free": _openrouter_provider(args.free_model),
            "openrouter-qwen": _openrouter_provider(args.qwen_model),
            "openrouter-qwen-max": _openrouter_provider(args.max_model),
            "openrouter-agent-qwen": {
                "kind": "agent_runtime",
                "runtime": "langgraph",
                "base_provider": "openrouter-qwen",
                "capabilities": ["repair"],
                "max_iterations": 3,
                "timeout_seconds": 90,
            },
        },
    }
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _ensure_generation_fixture_files(scratch: Path) -> None:
    docs = scratch / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    spec = docs / "spec.md"
    if not spec.exists():
        spec.write_text(
            """# Broken Counter Spec

The `broken_counter` module has an active-low reset. When `rst_n` is low, `count` resets to zero. On each rising edge after reset, `count` increments by one.
""",
            encoding="utf-8",
        )


def _openrouter_provider(model: str) -> dict[str, Any]:
    return {
        "kind": "openai_compatible",
        "capabilities": ["repair", "generation"],
        "base_url": OPENROUTER_BASE_URL,
        "endpoint": "chat/completions",
        "model": model,
        "api_key_env": "OPENROUTER_API_KEY",
        "timeout_seconds": 90,
    }


def _bounded(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n... truncated {len(value) - limit} chars"


def _markdown_report(summary: dict[str, Any]) -> str:
    lines = [
        "# OpenRouter Capability Study Summary",
        "",
        f"Status: `{summary['status']}`",
        f"Scratch root: `{summary['scratch_root']}`",
        "",
        "## Models",
    ]
    for name, model in summary["models"].items():
        lines.append(f"- `{name}`: `{model}`")
    lines.extend(["", "## Results", ""])
    lines.append("| Label | Status | Exit | Seconds |")
    lines.append("| --- | --- | ---: | ---: |")
    for item in summary["results"]:
        lines.append(f"| `{item['label']}` | `{item['status']}` | {item['exit_code']} | {item['elapsed_seconds']} |")
    lines.extend(["", "Raw stdout/stderr are bounded in the sibling JSON summary. Secrets are never persisted."])
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
