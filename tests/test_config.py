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


def test_init_project_uses_capability_defaults(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    assert config.default_provider_by_capability()["repair"] == "heuristic"
    assert config.default_provider_by_capability()["generation"] == "heuristic"
    assert config.provider_capabilities("heuristic") == ["repair", "generation"]


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


def test_config_rejects_absolute_external_roots(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["retrieval"]["external_roots"] = [str((sample_project / "docs").resolve())]
    write_json(config_path, payload)
    with pytest.raises(ConfigError):
        ProjectConfig.load(sample_project)
