from __future__ import annotations

import os
from pathlib import Path

import pytest

from telchines.config import ProjectConfig
from telchines.providers import check_provider_statuses
from telchines.utils import read_json, write_json


def test_openai_compatible_provider_live_check_when_env_configured(sample_project: Path) -> None:
    base_url = os.environ.get("TELCHINES_INTEGRATION_OPENAI_BASE_URL")
    model = os.environ.get("TELCHINES_INTEGRATION_OPENAI_MODEL")
    api_key = os.environ.get("TELCHINES_INTEGRATION_OPENAI_API_KEY")
    if not base_url or not model or not api_key:
        pytest.skip(
            "set TELCHINES_INTEGRATION_OPENAI_BASE_URL, TELCHINES_INTEGRATION_OPENAI_MODEL, "
            "and TELCHINES_INTEGRATION_OPENAI_API_KEY to run this live provider check"
        )

    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["model_mode"] = "hybrid"
    payload["project"]["model_policy"] = {
        "default_provider_by_capability": {"repair": "live-openai", "generation": "live-openai"},
        "providers": {
            "live-openai": {
                "kind": "openai_compatible",
                "capabilities": ["repair", "generation"],
                "base_url": base_url,
                "model": model,
                "api_key_env": "TELCHINES_INTEGRATION_OPENAI_API_KEY",
                "timeout_seconds": 30,
            }
        },
    }
    write_json(config_path, payload)

    config = ProjectConfig.load(sample_project)
    check = check_provider_statuses(config, "live-openai", live=True)[0]
    assert check.status == "passed", check.summary
    assert check.checks["transport"]["mode"] == "openai_compatible"
