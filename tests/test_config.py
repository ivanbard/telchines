from __future__ import annotations

from pathlib import Path

import pytest

from telchines.config import ProjectConfig
from telchines.errors import ConfigError, ProjectNotInitializedError
from telchines.utils import read_json, write_json


def test_config_discovery_from_nested_directory(sample_project: Path) -> None:
    nested = sample_project / "docs"
    config = ProjectConfig.discover(nested)
    assert config.project_root == sample_project


def test_config_rejects_invalid_retrieval_values(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["retrieval"]["chunk_lines"] = 0
    write_json(config_path, payload)
    with pytest.raises(ConfigError):
        ProjectConfig.load(sample_project)


def test_config_raises_outside_project(work_root: Path) -> None:
    with pytest.raises(ProjectNotInitializedError):
        ProjectConfig.discover(work_root)


def test_config_rejects_invalid_model_provider(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"]["providers"]["remote"] = {"kind": "openai_compatible", "capabilities": ["repair"], "model": "demo-model"}
    payload["project"]["model_policy"]["default_provider_by_capability"]["repair"] = "remote"
    write_json(config_path, payload)
    with pytest.raises(ConfigError):
        ProjectConfig.load(sample_project)


def test_config_rejects_invalid_openai_base_url(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"]["providers"]["remote"] = {
        "kind": "openai_compatible",
        "capabilities": ["repair"],
        "base_url": "localhost:11434/v1",
        "model": "demo-model",
    }
    payload["project"]["model_policy"]["default_provider_by_capability"]["repair"] = "remote"
    write_json(config_path, payload)
    with pytest.raises(ConfigError, match="base_url"):
        ProjectConfig.load(sample_project)


def test_config_rejects_invalid_openai_custom_headers(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"]["providers"]["remote"] = {
        "kind": "openai_compatible",
        "capabilities": ["repair"],
        "base_url": "http://127.0.0.1:9999/v1",
        "model": "demo-model",
        "headers": {"X-Trace": 42},
    }
    payload["project"]["model_policy"]["default_provider_by_capability"]["repair"] = "remote"
    write_json(config_path, payload)
    with pytest.raises(ConfigError, match="headers"):
        ProjectConfig.load(sample_project)


def test_config_rejects_openai_authorization_header_override(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"]["providers"]["remote"] = {
        "kind": "openai_compatible",
        "capabilities": ["repair"],
        "base_url": "http://127.0.0.1:9999/v1",
        "model": "demo-model",
        "headers": {"Authorization": "Bearer hardcoded"},
    }
    payload["project"]["model_policy"]["default_provider_by_capability"]["repair"] = "remote"
    write_json(config_path, payload)
    with pytest.raises(ConfigError, match="Authorization"):
        ProjectConfig.load(sample_project)


def test_config_rejects_absolute_openai_endpoint(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"]["providers"]["remote"] = {
        "kind": "openai_compatible",
        "capabilities": ["repair"],
        "base_url": "http://127.0.0.1:9999/v1",
        "model": "demo-model",
        "endpoint": "https://example.invalid/chat/completions",
    }
    payload["project"]["model_policy"]["default_provider_by_capability"]["repair"] = "remote"
    write_json(config_path, payload)
    with pytest.raises(ConfigError, match="endpoint"):
        ProjectConfig.load(sample_project)


def test_init_project_uses_capability_defaults(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    assert config.default_provider_by_capability()["repair"] == "heuristic"
    assert config.default_provider_by_capability()["generation"] == "heuristic"
    assert config.provider_capabilities("heuristic") == ["repair", "generation"]
    assert config.generation["sva"]["filename_template"] == "{module}_assertions.sv"
    assert config.generation["sva"]["validation_adapters"] == ["slang", "verilator"]
    assert config.generation["cocotb"]["test_file_template"] == "test_{module}.py"


def test_config_rejects_invalid_local_command_provider(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"]["providers"]["local-test"] = {
        "kind": "local_command",
        "capabilities": ["repair"],
        "args": ["tools/local_provider.py"],
    }
    payload["project"]["model_policy"]["default_provider_by_capability"]["repair"] = "local-test"
    write_json(config_path, payload)
    with pytest.raises(ConfigError):
        ProjectConfig.load(sample_project)


def test_config_rejects_invalid_local_command_output_limit(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"]["providers"]["local-test"] = {
        "kind": "local_command",
        "capabilities": ["repair"],
        "command": "python",
        "args": ["tools/local_provider.py"],
        "output_limit_chars": 64,
    }
    payload["project"]["model_policy"]["default_provider_by_capability"]["repair"] = "local-test"
    write_json(config_path, payload)
    with pytest.raises(ConfigError, match="output_limit_chars"):
        ProjectConfig.load(sample_project)


def test_config_accepts_agent_runtime_repair_provider(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"] = {
        "default_provider_by_capability": {"repair": "agent-repair", "generation": "heuristic"},
        "providers": {
            "heuristic": {"kind": "heuristic", "capabilities": ["generation"]},
            "local-repair": {
                "kind": "local_command",
                "capabilities": ["repair"],
                "command": "python",
                "args": ["tools/local_provider.py"],
                "timeout_seconds": 5,
            },
            "agent-repair": {
                "kind": "agent_runtime",
                "runtime": "langgraph",
                "base_provider": "local-repair",
                "capabilities": ["repair"],
                "max_iterations": 2,
                "timeout_seconds": 10,
            },
        },
    }
    write_json(config_path, payload)

    config = ProjectConfig.load(sample_project)

    assert config.default_provider_by_capability()["repair"] == "agent-repair"


def test_config_rejects_invalid_agent_runtime_provider(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"]["providers"]["agent-repair"] = {
        "kind": "agent_runtime",
        "runtime": "other",
        "base_provider": "heuristic",
        "capabilities": ["repair"],
    }
    payload["project"]["model_policy"]["default_provider_by_capability"]["repair"] = "agent-repair"
    write_json(config_path, payload)

    with pytest.raises(ConfigError, match="runtime"):
        ProjectConfig.load(sample_project)


def test_config_rejects_agent_runtime_missing_base_provider(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"]["providers"]["agent-repair"] = {
        "kind": "agent_runtime",
        "runtime": "langgraph",
        "base_provider": "missing",
        "capabilities": ["repair"],
    }
    payload["project"]["model_policy"]["default_provider_by_capability"]["repair"] = "agent-repair"
    write_json(config_path, payload)

    with pytest.raises(ConfigError, match="base_provider"):
        ProjectConfig.load(sample_project)


def test_config_rejects_agent_runtime_invalid_iterations(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"]["providers"]["local-repair"] = {
        "kind": "local_command",
        "capabilities": ["repair"],
        "command": "python",
        "args": ["tools/local_provider.py"],
    }
    payload["project"]["model_policy"]["providers"]["agent-repair"] = {
        "kind": "agent_runtime",
        "runtime": "langgraph",
        "base_provider": "local-repair",
        "capabilities": ["repair"],
        "max_iterations": 0,
    }
    payload["project"]["model_policy"]["default_provider_by_capability"]["repair"] = "agent-repair"
    write_json(config_path, payload)

    with pytest.raises(ConfigError, match="max_iterations"):
        ProjectConfig.load(sample_project)


def test_config_rejects_absolute_external_roots(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["retrieval"]["external_roots"] = [str((sample_project / "docs").resolve())]
    write_json(config_path, payload)
    with pytest.raises(ConfigError):
        ProjectConfig.load(sample_project)


def test_config_rejects_invalid_retrieval_patterns(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["retrieval"]["exclude_patterns"] = [str((sample_project / "logs").resolve())]
    write_json(config_path, payload)
    with pytest.raises(ConfigError, match="exclude_patterns"):
        ProjectConfig.load(sample_project)


def test_config_rejects_invalid_retrieval_aliases(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["retrieval"]["aliases"] = {"start bit": ["serial_i", ""]}
    write_json(config_path, payload)
    with pytest.raises(ConfigError, match="retrieval.aliases"):
        ProjectConfig.load(sample_project)


def test_config_rejects_invalid_generation_conventions(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["generation"] = {
        "sva": {
            "output_dir": ".tel/artifacts/generated",
            "filename_template": "../{module}_assertions.sv",
        }
    }
    write_json(config_path, payload)
    with pytest.raises(ConfigError, match="generation.sva.filename_template"):
        ProjectConfig.load(sample_project)


def test_config_rejects_non_object_generation_config(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["generation"] = "generated"
    write_json(config_path, payload)
    with pytest.raises(ConfigError, match="generation must be an object"):
        ProjectConfig.load(sample_project)


def test_config_rejects_invalid_sva_validation_adapters(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["generation"] = {"sva": {"validation_adapters": ["slang", ""]}}
    write_json(config_path, payload)
    with pytest.raises(ConfigError, match="generation.sva.validation_adapters"):
        ProjectConfig.load(sample_project)


def test_config_validates_generation_max_attempts(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    assert config.generation["sva"]["max_attempts"] == 1
    assert config.generation["cocotb"]["max_attempts"] == 1

    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["generation"] = {"cocotb": {"max_attempts": 0}}
    write_json(config_path, payload)
    with pytest.raises(ConfigError, match="generation.cocotb.max_attempts"):
        ProjectConfig.load(sample_project)


def test_config_loads_utf8_bom_json(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text("\ufeff" + text, encoding="utf-8")
    assert ProjectConfig.load(sample_project).project_root == sample_project
