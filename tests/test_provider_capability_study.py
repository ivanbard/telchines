from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "provider_capability_study.py"
SPEC = importlib.util.spec_from_file_location("provider_capability_study", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
provider_capability_study = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provider_capability_study)


def test_provider_capability_matrix_presets_are_valid() -> None:
    for path in sorted((REPO_ROOT / "docs" / "provider-matrices").glob("*.json")):
        matrix = provider_capability_study.load_matrix(path)
        assert matrix["schema_version"] == "0.1"
        assert matrix["providers"]


def test_provider_capability_matrix_rejects_unknown_kind(work_root: Path) -> None:
    matrix = {
        "schema_version": "0.1",
        "name": "bad",
        "providers": [
            {
                "name": "bad-provider",
                "kind": "magic",
                "capabilities": ["repair"],
            }
        ],
    }
    path = work_root / "bad.json"
    path.write_text(json.dumps(matrix), encoding="utf-8")
    with pytest.raises(provider_capability_study.MatrixError, match="unsupported kind"):
        provider_capability_study.load_matrix(path)


def test_provider_capability_matrix_rejects_literal_secret_field(work_root: Path) -> None:
    matrix = {
        "schema_version": "0.1",
        "name": "bad",
        "providers": [
            {
                "name": "bad-provider",
                "kind": "openai_compatible",
                "capabilities": ["repair"],
                "base_url": "https://example.test/v1",
                "model": "demo",
                "api_key": "literal-secret",
            }
        ],
    }
    path = work_root / "bad.json"
    path.write_text(json.dumps(matrix), encoding="utf-8")
    with pytest.raises(provider_capability_study.MatrixError, match="literal secret"):
        provider_capability_study.load_matrix(path)


def test_provider_capability_matrix_expands_agent_base_dependency() -> None:
    matrix = provider_capability_study.load_matrix(REPO_ROOT / "docs" / "provider-matrices" / "agent_runtime.json")
    selected = provider_capability_study._selected_providers(matrix, "matrix-agent")
    assert [provider["name"] for provider in selected] == ["matrix-local-base", "matrix-agent"]


def test_provider_capability_matrix_accepts_anthropic_preset() -> None:
    matrix = provider_capability_study.load_matrix(REPO_ROOT / "docs" / "provider-matrices" / "anthropic.json")
    assert matrix["providers"][0]["kind"] == "anthropic"


def test_provider_capability_repair_commands_select_provider(work_root: Path) -> None:
    matrix = provider_capability_study.load_matrix(REPO_ROOT / "docs" / "provider-matrices" / "local_command.json")
    plan = provider_capability_study.build_plan(matrix, matrix["providers"], work_root, include_live=False, dry_run=True)
    repair_command = next(command for command in plan["commands"] if command["label"] == "agent_repair")
    assert repair_command["command"][-2:] == ["--provider", "matrix-local"]
