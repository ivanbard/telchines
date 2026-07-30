from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from telchines.errors import ConfigError
from telchines.utils import stable_id, utc_now


def certify_providers(manifest_path: Path, *, include_live: bool) -> dict[str, object]:
    """Run a bounded, explicitly enabled provider certification manifest."""
    manifest_path = manifest_path.resolve()
    manifest = _load_manifest(manifest_path)
    gate = str(manifest["live_gate_env"])
    if not include_live:
        raise ConfigError("live certification requires --include-live")
    if os.environ.get(gate) != "1":
        raise ConfigError(f"live certification is disabled; set {gate}=1 and rerun with --include-live")
    matrix = (manifest_path.parent / str(manifest["matrix"])).resolve()
    provider = str(manifest["provider"])
    max_requests = int(manifest["max_requests"])
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "scripts" / "provider_capability_study.py"),
        "--matrix",
        str(matrix),
        "--provider",
        provider,
        "--include-live",
        "--repeat-count",
        str(manifest["repeat_count"]),
        "--max-live-commands",
        str(max_requests),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=int(manifest["timeout_seconds"]))
    payload = _last_json(completed.stdout)
    return {
        "certification_id": stable_id("cert", manifest["suite_version"], provider, utc_now()),
        "status": "passed" if completed.returncode == 0 and payload.get("status") == "passed" else "failed",
        "suite_version": manifest["suite_version"],
        "provider": provider,
        "model": manifest["model"],
        "repeat_count": manifest["repeat_count"],
        "budget": {
            "max_requests": max_requests,
            "max_output_tokens": manifest["max_output_tokens"],
            "max_cost_usd": manifest["max_cost_usd"],
            "enforcement": "hard request cap; token and cost ceilings are release-approval limits recorded with this run",
        },
        "command": command[1:],
        "study": payload,
        "stderr": "provider-study diagnostics captured locally but omitted from the certification payload" if completed.stderr else "",
    }


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"certification manifest does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"certification manifest is not valid JSON: {exc}") from exc
    required = {"schema_version", "suite_version", "matrix", "provider", "model", "live_gate_env", "repeat_count", "max_requests", "max_output_tokens", "max_cost_usd", "timeout_seconds"}
    missing = sorted(required.difference(payload)) if isinstance(payload, dict) else sorted(required)
    if missing or not isinstance(payload, dict) or payload.get("schema_version") != "0.1":
        raise ConfigError(f"invalid certification manifest; missing or invalid fields: {', '.join(missing or ['schema_version'])}")
    if int(payload["repeat_count"]) < 3:
        raise ConfigError("certification repeat_count must be at least 3")
    if int(payload["max_requests"]) < int(payload["repeat_count"]):
        raise ConfigError("certification max_requests must cover every repeat")
    if int(payload["max_output_tokens"]) < 1 or float(payload["max_cost_usd"]) <= 0 or int(payload["timeout_seconds"]) < 1:
        raise ConfigError("certification budgets and timeout must be positive")
    return payload


def _last_json(output: str) -> dict[str, object]:
    start = output.rfind("{")
    if start < 0:
        return {"status": "invalid_runner_output"}
    try:
        value = json.loads(output[start:])
    except json.JSONDecodeError:
        return {"status": "invalid_runner_output"}
    return value if isinstance(value, dict) else {"status": "invalid_runner_output"}

