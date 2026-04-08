from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telchines.errors import ConfigError, ProjectNotInitializedError
from telchines.models import VerificationProject
from telchines.utils import dataclass_to_dict, ensure_directory, read_json, stable_id, utc_now, write_json

SUPPORTED_MODEL_MODES = {"local", "hybrid", "remote"}
SUPPORTED_ADAPTERS = {"verilator", "iverilog", "verible", "symbiyosys", "fixture"}


@dataclass(slots=True)
class ProjectConfig:
    project: VerificationProject
    store_dir: str = ".tel"
    index_dir: str = ".tel/index"
    artifacts_dir: str = ".tel/artifacts"
    model_mode: str = "hybrid"
    no_egress: bool = False
    adapters: list[str] = field(default_factory=lambda: ["verilator", "iverilog", "verible", "symbiyosys"])
    retrieval: dict[str, Any] = field(default_factory=lambda: {"chunk_lines": 20, "max_hits": 5})

    @property
    def project_root(self) -> Path:
        return Path(self.project.root_path)

    @property
    def config_path(self) -> Path:
        return self.project_root / self.project.config_path

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": dataclass_to_dict(self.project),
            "store_dir": self.store_dir,
            "index_dir": self.index_dir,
            "artifacts_dir": self.artifacts_dir,
            "model_mode": self.model_mode,
            "no_egress": self.no_egress,
            "adapters": self.adapters,
            "retrieval": self.retrieval,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProjectConfig":
        config = cls(
            project=VerificationProject(**payload["project"]),
            store_dir=payload["store_dir"],
            index_dir=payload["index_dir"],
            artifacts_dir=payload["artifacts_dir"],
            model_mode=payload["model_mode"],
            no_egress=payload["no_egress"],
            adapters=payload["adapters"],
            retrieval=payload["retrieval"],
        )
        config.validate()
        return config

    @classmethod
    def init_project(cls, root: Path, name: str | None = None) -> "ProjectConfig":
        root = root.resolve()
        ensure_directory(root)
        ensure_directory(root / ".tel")
        project_name = name or root.name
        project = VerificationProject(
            project_id=stable_id("proj", str(root)),
            name=project_name,
            root_path=str(root),
            created_at=utc_now(),
        )
        config = cls(project=project)
        config.save()
        return config

    @classmethod
    def load(cls, root: Path) -> "ProjectConfig":
        config_path = root.resolve() / ".tel" / "config.json"
        if not config_path.exists():
            raise ProjectNotInitializedError(f"no Telchines project found at {root.resolve()}")
        return cls.from_dict(read_json(config_path))

    @classmethod
    def discover(cls, start: Path) -> "ProjectConfig":
        current = start.resolve()
        for candidate in [current, *current.parents]:
            config_path = candidate / ".tel" / "config.json"
            if config_path.exists():
                return cls.from_dict(read_json(config_path))
        raise ProjectNotInitializedError(
            f"no Telchines project found from {start.resolve()} upward; run `tel project init` in a repository root"
        )

    def validate(self) -> None:
        if not self.project.root_path:
            raise ConfigError("project.root_path must be set")
        root = self.project_root.resolve()
        if not root.exists() or not root.is_dir():
            raise ConfigError(f"project root does not exist: {root}")
        if self.model_mode not in SUPPORTED_MODEL_MODES:
            raise ConfigError(f"model_mode must be one of: {', '.join(sorted(SUPPORTED_MODEL_MODES))}")
        if not isinstance(self.no_egress, bool):
            raise ConfigError("no_egress must be a boolean")
        if not isinstance(self.adapters, list) or not self.adapters:
            raise ConfigError("adapters must be a non-empty list")
        invalid_adapters = [adapter for adapter in self.adapters if adapter not in SUPPORTED_ADAPTERS]
        if invalid_adapters:
            raise ConfigError(f"unsupported adapters: {', '.join(invalid_adapters)}")
        if not isinstance(self.retrieval, dict):
            raise ConfigError("retrieval must be an object")
        for key in ("chunk_lines", "max_hits"):
            value = self.retrieval.get(key)
            if not isinstance(value, int) or value <= 0:
                raise ConfigError(f"retrieval.{key} must be a positive integer")
        for path_value, field_name in (
            (self.store_dir, "store_dir"),
            (self.index_dir, "index_dir"),
            (self.artifacts_dir, "artifacts_dir"),
        ):
            if not isinstance(path_value, str) or not path_value.strip():
                raise ConfigError(f"{field_name} must be a non-empty string")
            if Path(path_value).is_absolute():
                raise ConfigError(f"{field_name} must be relative to the project root")

    def save(self) -> None:
        self.validate()
        write_json(self.config_path, self.to_dict())
