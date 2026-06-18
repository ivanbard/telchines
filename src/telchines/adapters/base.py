from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telchines.adapters.parsing import parse_common_output
from telchines.errors import AdapterExecutionError
from telchines.models import AdapterDescriptor, Observation, ToolReference
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
    summary: str
    artifacts: dict[str, str] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)


class ToolAdapter:
    name = "base"
    kind = "tool"
    category = "tool"
    validation_mode = "compile_only"
    binary_names: tuple[str, ...] = ()
    required_binaries: tuple[str, ...] = ()
    supported_workflows: tuple[str, ...] = ()
    artifact_types: tuple[str, ...] = ("log",)

    def is_available(self) -> bool:
        required = self.required_binaries or self.binary_names
        if not required:
            return True
        return all(shutil.which(binary) for binary in required)

    def version(self) -> str:
        binary = next(iter(self.required_binaries or self.binary_names), "")
        if not binary or shutil.which(binary) is None:
            return "unavailable"
        for flag in ("--version", "-V", "-version"):
            try:
                result = subprocess.run([binary, flag], capture_output=True, text=True, check=False, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                continue
            output = (result.stdout or result.stderr).strip()
            if result.returncode == 0 and output:
                return output.splitlines()[0].strip()
        return "unknown"

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        raise NotImplementedError

    def parse_output(self, run_id: str, text: str) -> list[Observation]:
        return parse_common_output(run_id, text)

    def parse_result(self, project_root: Path, files: list[str], stdout: str, stderr: str, combined: str) -> dict[str, Any]:
        return {}

    def collect_artifacts(
        self,
        project_root: Path,
        files: list[str],
        artifacts_dir: Path,
        run_id: str,
        stdout: str,
        stderr: str,
        combined: str,
    ) -> dict[str, str]:
        return {}

    def describe(self, *, enabled: bool = False) -> AdapterDescriptor:
        return AdapterDescriptor(
            name=self.name,
            kind=self.kind,
            category=self.category,
            validation_mode=self.validation_mode,
            binary_names=list(self.binary_names),
            required_binaries=list(self.required_binaries or self.binary_names),
            supported_workflows=list(self.supported_workflows),
            artifact_types=list(self.artifact_types),
            available=self.is_available(),
            enabled=enabled,
            version=self.version(),
        )

    def run(self, run_id: str, project_root: Path, files: list[str], artifacts_dir: Path, extra_args: list[str] | None = None) -> AdapterExecution:
        command = self.build_command(project_root, files, extra_args)
        if not files:
            raise AdapterExecutionError(f"{self.name} requires at least one input file")
        if not self.is_available():
            binaries = ", ".join(self.binary_names) or self.name
            raise AdapterExecutionError(f"{self.name} is not available on PATH; expected one of: {binaries}")
        started_at = utc_now()
        try:
            process = subprocess.run(command, cwd=project_root, capture_output=True, text=True, check=False)
        except OSError as exc:
            raise AdapterExecutionError(f"failed to execute {self.name}: {exc}") from exc
        finished_at = utc_now()
        ensure_directory(artifacts_dir)
        log_path = artifacts_dir / f"{run_id}.log"
        combined = process.stdout + process.stderr
        log_path.write_text(combined, encoding="utf-8")
        observations = self.parse_output(run_id, combined)
        result = self.parse_result(project_root, files, process.stdout, process.stderr, combined)
        artifacts = {"log_path": str(log_path)}
        artifacts.update(self.collect_artifacts(project_root, files, artifacts_dir, run_id, process.stdout, process.stderr, combined))
        summary = f"{self.name} exited with code {process.returncode}"
        normalized_status = str(result.get("status", "")).strip()
        if normalized_status:
            summary = f"{summary}; status: {normalized_status}"
        if observations:
            summary = f"{summary}; first observation: {observations[0].signature}"
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
            summary=summary,
            artifacts=artifacts,
            result=result,
        )

    @property
    def tool_reference(self) -> ToolReference:
        return ToolReference(kind=self.kind, name=self.name, version=self.version())
