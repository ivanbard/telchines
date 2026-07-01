from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = REPO_ROOT / "docs" / "provider-matrices" / "openrouter.json"
DEFAULT_SCRATCH_ROOT = REPO_ROOT / ".test-work" / "openrouter-capability-study"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compatibility wrapper for the OpenRouter provider capability study.")
    parser.add_argument("--dry-run", action="store_true", help="List the OpenRouter study matrix without live calls.")
    parser.add_argument("--scratch-root", type=Path, default=DEFAULT_SCRATCH_ROOT)
    parser.add_argument("--free-model", default="cohere/north-mini-code:free", help="Accepted for compatibility; use provider matrix presets for multi-model studies.")
    parser.add_argument("--qwen-model", default="qwen/qwen3.7-plus")
    parser.add_argument("--max-model", default="qwen/qwen3.7-max", help="Accepted for compatibility; use provider matrix presets for escalation studies.")
    args = parser.parse_args()

    env = os.environ.copy()
    env.setdefault("TELCHINES_OPENROUTER_MODEL", args.qwen_model)
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "provider_capability_study.py"),
        "--matrix",
        str(DEFAULT_MATRIX),
        "--scratch-root",
        str(args.scratch_root.parent),
    ]
    if args.dry_run:
        command.append("--dry-run")
    elif os.environ.get("OPENROUTER_API_KEY"):
        env["TELCHINES_LIVE_OPENROUTER"] = "1"
        command.append("--include-live")
    else:
        print('{\n  "status": "skipped_missing_key",\n  "missing_env": "OPENROUTER_API_KEY"\n}')
        return 0

    process = subprocess.run(command, cwd=REPO_ROOT, env=env, text=True, check=False)
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
