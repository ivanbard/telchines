from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ovai.models import VerificationProject
from ovai.utils import dataclass_to_dict, ensure_directory, read_json, stable_id, utc_now, write_json


@dataclass(slots=True)
class ProjectConfig:
    project: VerificationProject
    store_dir: str = ".ovai"
    index_dir: str = ".ovai/index"
    artifacts_dir: str = ".ovai/artifacts"
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
        return cls(
            project=VerificationProject(**payload["project"]),
            store_dir=payload["store_dir"],
            index_dir=payload["index_dir"],
            artifacts_dir=payload["artifacts_dir"],
            model_mode=payload["model_mode"],
            no_egress=payload["no_egress"],
            adapters=payload["adapters"],
            retrieval=payload["retrieval"],
        )

    @classmethod
    def init_project(cls, root: Path, name: str | None = None) -> "ProjectConfig":
        root = root.resolve()
        ensure_directory(root / ".ovai")
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
        return cls.from_dict(read_json(root.resolve() / ".ovai" / "config.json"))

    def save(self) -> None:
        write_json(self.config_path, self.to_dict())
