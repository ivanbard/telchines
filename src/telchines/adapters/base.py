from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from telchines.adapters.parsing import parse_common_output
from telchines.models import Observation, ToolReference
from telchines.utils import ensure_directory, utc_now


@dataclass(slots=True)
class AdapterExecution:
    command: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    log_path: str
    started_at: str
    finished_at: str
    observations: list[Observation]


class ToolAdapter:
    name = "base"
    kind = "tool"
    binary_names: tuple[str, ...] = ()

    def is_available(self) -> bool:
        return any(shutil.which(binary) for binary in self.binary_names)

    def version(self) -> str:
        return "unknown"

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        raise NotImplementedError

    def parse_output(self, run_id: str, text: str) -> list[Observation]:
        return parse_common_output(run_id, text)

    def run(self, run_id: str, project_root: Path, files: list[str], artifacts_dir: Path, extra_args: list[str] | None = None) -> AdapterExecution:
        command = self.build_command(project_root, files, extra_args)
        started_at = utc_now()
        process = subprocess.run(command, cwd=project_root, capture_output=True, text=True, check=False)
        finished_at = utc_now()
        ensure_directory(artifacts_dir)
        log_path = artifacts_dir / f"{run_id}.log"
        combined = process.stdout + process.stderr
        log_path.write_text(combined, encoding="utf-8")
        observations = self.parse_output(run_id, combined)
        return AdapterExecution(
            command=command,
            cwd=str(project_root),
            exit_code=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            log_path=str(log_path),
            started_at=started_at,
            finished_at=finished_at,
            observations=observations,
        )

    @property
    def tool_reference(self) -> ToolReference:
        return ToolReference(kind=self.kind, name=self.name, version=self.version())
