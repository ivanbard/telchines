from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MATRIX_DIR = REPO_ROOT / "docs" / "provider-matrices"


def test_live_provider_matrices_when_env_configured() -> None:
    enabled = _enabled_live_providers()
    if not enabled:
        pytest.skip("set a provider matrix TELCHINES_LIVE_* env gate plus credentials to run live provider checks")

    run_workflows = os.environ.get("TELCHINES_LIVE_PROVIDER_WORKFLOWS") == "1"
    for matrix_path, provider_name in enabled:
        command = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "provider_capability_study.py"),
            "--matrix",
            str(matrix_path),
            "--provider",
            provider_name,
            "--include-live",
        ]
        if not run_workflows:
            command.extend(["--max-live-commands", "2"])
        result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, check=False, timeout=240)
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout[result.stdout.rfind("{") :])
        assert payload["status"] in {"passed", "skipped"}


def _enabled_live_providers() -> list[tuple[Path, str]]:
    enabled: list[tuple[Path, str]] = []
    for matrix_path in sorted(MATRIX_DIR.glob("*.json")):
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        for provider in matrix.get("providers", []):
            if provider.get("kind") == "agent_runtime":
                continue
            if not provider.get("live"):
                continue
            enabled_env = provider.get("enabled_env")
            if isinstance(enabled_env, str) and os.environ.get(enabled_env) == "1":
                enabled.append((matrix_path, provider["name"]))
    return enabled
