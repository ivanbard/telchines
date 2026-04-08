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
    payload["project"]["model_policy"]["providers"]["remote"] = {"kind": "openai_compatible", "model": "demo-model"}
    payload["project"]["model_policy"]["repair_provider"] = "remote"
    write_json(config_path, payload)
    with pytest.raises(ConfigError):
        ProjectConfig.load(sample_project)
