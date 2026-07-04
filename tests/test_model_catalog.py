from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from telchines.config import ProjectConfig, SUPPORTED_REASONING_LEVELS
from telchines.errors import ConfigError
from telchines.model_catalog import (
    list_model_options,
    provider_model_metadata,
    set_default_provider,
    set_provider_model,
    set_provider_reasoning,
)
from telchines.utils import read_json, write_json


PROVIDER_NAME = st.from_regex(r"[A-Za-z][A-Za-z0-9_-]{0,12}", fullmatch=True).filter(lambda value: value not in {"heuristic", "missing"})
MODEL_TEXT = st.from_regex(r"[A-Za-z0-9_.:/-]{1,24}", fullmatch=True)
PADDED_MODEL_TEXT = st.builds(lambda left, value, right: f"{left}{value}{right}", st.text(" \t", max_size=3), MODEL_TEXT, st.text(" \t", max_size=3))
INVALID_REASONING_LEVEL = st.text(min_size=0, max_size=16).filter(lambda value: value.strip() not in SUPPORTED_REASONING_LEVELS)

REQUIRED_METADATA_KEYS = {
    "provider",
    "model",
    "model_source",
    "reasoning_level",
    "reasoning_supported",
    "reasoning_wire_format",
    "model_warnings",
}


def _install_policy(project_root: Path, providers: dict[str, dict[str, Any]], defaults: dict[str, str] | None = None) -> ProjectConfig:
    config_path = project_root / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"] = {
        "default_provider_by_capability": defaults or {"repair": "local-test", "generation": "heuristic"},
        "providers": providers,
    }
    write_json(config_path, payload)
    return ProjectConfig.load(project_root)


def _local_provider(**extra: Any) -> dict[str, Any]:
    return {
        "kind": "local_command",
        "capabilities": ["repair"],
        "command": "python",
        "args": ["tools/local_provider.py"],
        "timeout_seconds": 5,
        **extra,
    }


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(model=PADDED_MODEL_TEXT)
def test_set_provider_model_trims_and_preserves_unrelated_fields(sample_project: Path, model: str) -> None:
    config = _install_policy(
        sample_project,
        {
            "heuristic": {"kind": "heuristic", "capabilities": ["generation"]},
            "local-test": _local_provider(custom_metadata={"keep": True}),
        },
    )

    payload = set_provider_model(config, "local-test", model)

    persisted = read_json(sample_project / ".tel" / "config.json")
    provider = persisted["project"]["model_policy"]["providers"]["local-test"]
    assert payload["model"] == model.strip()
    assert provider["model"] == model.strip()
    assert provider["model_source"] == "manual"
    assert provider["custom_metadata"] == {"keep": True}
    assert provider["command"] == "python"


@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(model=st.text(" \t\r\n", max_size=8))
def test_set_provider_model_rejects_blank_models(sample_project: Path, model: str) -> None:
    config = _install_policy(sample_project, {"local-test": _local_provider()}, {"repair": "local-test"})

    with pytest.raises(ConfigError, match="model must be non-empty"):
        set_provider_model(config, "local-test", model)


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(level=st.sampled_from(sorted(SUPPORTED_REASONING_LEVELS)))
def test_set_provider_reasoning_accepts_supported_levels(sample_project: Path, level: str) -> None:
    config = _install_policy(sample_project, {"local-test": _local_provider()}, {"repair": "local-test"})

    payload = set_provider_reasoning(config, "local-test", level)

    persisted = read_json(sample_project / ".tel" / "config.json")
    assert payload["reasoning_level"] == level
    assert persisted["project"]["model_policy"]["providers"]["local-test"]["reasoning_level"] == level


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(level=INVALID_REASONING_LEVEL)
def test_set_provider_reasoning_rejects_unsupported_levels(sample_project: Path, level: str) -> None:
    config = _install_policy(sample_project, {"local-test": _local_provider()}, {"repair": "local-test"})

    with pytest.raises(ConfigError, match="reasoning level"):
        set_provider_reasoning(config, "local-test", level.strip())


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(provider_name=PROVIDER_NAME, capability=st.sampled_from(["repair", "generation"]))
def test_set_default_provider_requires_requested_capability(sample_project: Path, provider_name: str, capability: str) -> None:
    other_capability = "generation" if capability == "repair" else "repair"
    config = _install_policy(
        sample_project,
        {
            "heuristic": {"kind": "heuristic", "capabilities": [other_capability]},
            provider_name: _local_provider(capabilities=[capability]),
        },
        {"repair": "heuristic", "generation": "heuristic"},
    )

    payload = set_default_provider(config, capability, provider_name)

    assert payload["default_provider_by_capability"][capability] == provider_name
    with pytest.raises(ConfigError, match=f"does not support {other_capability}"):
        set_default_provider(ProjectConfig.load(sample_project), other_capability, provider_name)


@pytest.mark.parametrize(
    ("provider_name", "provider_config", "providers"),
    [
        ("heuristic", {"kind": "heuristic", "capabilities": ["repair", "generation"]}, None),
        (
            "openai",
            {
                "kind": "openai_compatible",
                "capabilities": ["repair"],
                "base_url": "http://127.0.0.1:9999/v1",
                "model": "gpt-test-latest",
                "reasoning_level": "high",
            },
            None,
        ),
        ("anthropic", {"kind": "anthropic", "capabilities": ["repair"], "model": "claude-test", "reasoning_level": "low"}, None),
        ("local", _local_provider(model="wrapper-test", reasoning_level="medium"), None),
        (
            "agent",
            {"kind": "agent_runtime", "runtime": "langgraph", "base_provider": "base", "capabilities": ["repair"]},
            {"base": _local_provider(model="base-model", reasoning_level="high")},
        ),
    ],
)
def test_provider_model_metadata_has_required_keys(provider_name: str, provider_config: dict[str, Any], providers: dict[str, Any] | None) -> None:
    metadata = provider_model_metadata(provider_name, provider_config, providers)

    assert REQUIRED_METADATA_KEYS <= set(metadata)
    assert metadata["provider"] == provider_name
    assert isinstance(metadata["model_warnings"], list)


def test_agent_runtime_metadata_delegates_base_and_warns_when_missing() -> None:
    provider_config = {"kind": "agent_runtime", "runtime": "langgraph", "base_provider": "base", "capabilities": ["repair"]}
    delegated = provider_model_metadata("agent", provider_config, {"base": _local_provider(model="base-model", reasoning_level="high")})
    missing = provider_model_metadata("agent", provider_config, {})

    assert delegated["model"] == "base-model"
    assert delegated["reasoning_level"] == "high"
    assert delegated["model_source"] == "delegated"
    assert missing["model_source"] == "delegated"
    assert "base provider is not configured" in missing["model_warnings"][0]


def test_list_model_options_offline_uses_configured_presets_and_wrapper_models(sample_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_urlopen(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("offline model listing must not call the network")

    monkeypatch.setattr("telchines.model_catalog.request.urlopen", fail_urlopen)
    config = _install_policy(
        sample_project,
        {
            "heuristic": {"kind": "heuristic", "capabilities": ["generation"]},
            "local-test": _local_provider(),
            "remote": {
                "kind": "openai_compatible",
                "capabilities": ["repair"],
                "base_url": "http://127.0.0.1:9999/v1",
                "model": "configured-model",
                "api_key_env": "TELCHINES_TEST_API_KEY",
            },
        },
        {"repair": "remote", "generation": "heuristic"},
    )

    payload = list_model_options(config, live=False)

    providers = {item["name"]: item for item in payload["providers"]}
    assert providers["remote"]["discovery_status"] == "skipped"
    assert providers["remote"]["models"][0] == "configured-model"
    assert "gpt-4.1" in providers["remote"]["models"]
    assert providers["local-test"]["models"] == ["wrapper-managed"]
    assert providers["heuristic"]["models"] == ["heuristic"]
