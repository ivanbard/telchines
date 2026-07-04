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


def test_provider_capability_repeat_count_adds_stability_metadata(work_root: Path) -> None:
    matrix = provider_capability_study.load_matrix(REPO_ROOT / "docs" / "provider-matrices" / "local_command.json")
    plan = provider_capability_study.build_plan(matrix, matrix["providers"], work_root, include_live=False, dry_run=True, repeat_count=2)
    repair_commands = [command for command in plan["commands"] if command["label"] == "agent_repair"]
    assert [command["repeat_index"] for command in repair_commands] == [1, 2]
    assert all(command["repeat_count"] == 2 for command in repair_commands)
    assert all(command["provider_kind"] == "local_command" for command in repair_commands)


def test_provider_capability_repeat_count_must_be_positive(work_root: Path) -> None:
    matrix = provider_capability_study.load_matrix(REPO_ROOT / "docs" / "provider-matrices" / "local_command.json")

    with pytest.raises(provider_capability_study.MatrixError, match="repeat-count"):
        provider_capability_study.build_plan(matrix, matrix["providers"], work_root, include_live=False, dry_run=True, repeat_count=0)


def test_provider_capability_provider_config_copies_model_selection_fields() -> None:
    provider = {
        "name": "remote",
        "kind": "openai_compatible",
        "capabilities": ["repair"],
        "base_url": "http://127.0.0.1:9999/v1",
        "model": "configured-model",
        "api_key_env": "TELCHINES_TEST_API_KEY",
        "reasoning_level": "high",
        "reasoning_summary": "concise",
        "reasoning_wire_format": "openai_chat",
        "model_source": "manual",
        "supports_reasoning_effort": True,
    }

    config = provider_capability_study._provider_config(provider)

    assert config["model"] == "configured-model"
    assert config["reasoning_level"] == "high"
    assert config["reasoning_summary"] == "concise"
    assert config["reasoning_wire_format"] == "openai_chat"
    assert config["model_source"] == "manual"
    assert config["supports_reasoning_effort"] is True


def test_provider_capability_markdown_report_includes_model_stability_fields(work_root: Path) -> None:
    summary = {
        "matrix": "demo",
        "status": "passed",
        "scratch_root": str(work_root),
        "providers": [
            {
                "name": "remote",
                "kind": "openai_compatible",
                "model": "configured-model",
                "reasoning_level": "high",
                "status": "planned",
                "reason": "",
            }
        ],
        "results": [
            {
                "provider": "remote",
                "label": "provider_check",
                "repeat_index": 1,
                "model": "configured-model",
                "reasoning_level": "high",
                "status": "passed",
                "exit_code": 0,
                "elapsed_seconds": 0.1,
                "validation_status": None,
                "candidate_id": None,
                "attempt_count": None,
                "semantic_fingerprint": "abc123",
            }
        ],
        "metrics": {
            "stability": [
                {
                    "provider": "remote",
                    "label": "provider_check",
                    "model": "configured-model",
                    "reasoning_level": "high",
                    "runs": 1,
                    "stable": True,
                    "statuses": ["passed"],
                    "fingerprints": ["abc123"],
                }
            ]
        },
    }

    rendered = provider_capability_study._markdown_report(summary)

    assert "| Provider | Kind | Model | Reasoning | Status | Reason |" in rendered
    assert "| Provider | Scenario | Repeat | Model | Reasoning | Status |" in rendered
    assert "## Stability" in rendered
    assert "configured-model" in rendered
    assert "abc123" in rendered


def test_provider_capability_redaction_removes_secret_env_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELCHINES_TEST_API_KEY", "super-secret-token")
    summary = {
        "providers": [{"name": "remote", "model_warnings": ["safe"]}],
        "results": [{"stdout": "super-secret-token", "stderr": "Authorization: super-secret-token"}],
    }

    redacted = provider_capability_study._redact_summary(summary)

    encoded = json.dumps(redacted)
    assert "super-secret-token" not in encoded
    assert encoded.count("[REDACTED]") >= 1


def test_provider_capability_scorer_rejects_false_green_agent_repair() -> None:
    status, reason = provider_capability_study._score_command_result(
        {"label": "agent_repair"},
        0,
        {"status": "failed", "result": {"patch_id": None, "validation_status": None}},
    )

    assert status == "failed"
    assert reason == "agent_repair_missing_validated_patch"


def test_provider_capability_skips_missing_openai_model_before_scratch(work_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("TELCHINES_LIVE_OPENAI", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("TELCHINES_OPENAI_MODEL", raising=False)
    matrix = provider_capability_study.load_matrix(REPO_ROOT / "docs" / "provider-matrices" / "openai.json")

    plan = provider_capability_study.build_plan(matrix, matrix["providers"], work_root, include_live=True)

    assert all(command["status"] == "skipped" for command in plan["commands"])
    assert any(command["reason"] == "missing_env:TELCHINES_OPENAI_MODEL" for command in plan["commands"])
    assert any(str(command["reason"]).startswith("base_provider_skipped:openai:") for command in plan["commands"])
    assert provider_capability_study._active_providers(matrix["providers"], plan["commands"]) == []
