from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telchines.adapters.parsing import parse_common_output
from telchines.errors import AdapterExecutionError
from telchines.models import AdapterDescriptor, Observation, ToolReference
from telchines.utils import SECRET_KEY_RE, ensure_directory, unique_preserve_order, utc_now


def executable_status(binary: str) -> tuple[bool, str, str]:
    """Return whether *binary* is runnable here, its version, and a diagnostic.

    PATH can be inherited across Windows/WSL boundaries.  A name resolving there
    is not evidence that the executable is usable by the current Python host.
    """
    resolved = shutil.which(binary)
    if not resolved:
        return False, "unavailable", "not found on PATH"
    if _is_wsl() and _is_windows_mount_path(resolved):
        return False, "unavailable", f"incompatible cross-host executable at {resolved}"
    for flag in ("--version", "-V", "-version"):
        try:
            result = subprocess.run([binary, flag], capture_output=True, text=True, check=False, timeout=5)
        except TypeError:
            continue
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, "unavailable", f"version probe failed: {exc}"
        output = (result.stdout or result.stderr).strip()
        if result.returncode == 0 and output:
            return True, output.splitlines()[0].strip(), ""
    return False, "unavailable", "version probe did not return a usable version"


def _is_wsl() -> bool:
    if os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
        return True
    if sys.platform != "linux":
        return False
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _is_windows_mount_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return normalized.startswith("/mnt/") and len(normalized) > 6 and normalized[6] == "/"


@dataclass(slots=True)
class AdapterRunSpec:
    files: list[str] = field(default_factory=list)
    filelists: list[str] = field(default_factory=list)
    include_dirs: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    top_module: str | None = None
    work_library: str | None = None
    standard: str = "systemverilog"
    timeout_seconds: int | None = None
    extra_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_legacy(cls, files: list[str], extra_args: list[str] | None = None) -> "AdapterRunSpec":
        return cls(files=list(files), extra_args=list(extra_args or []))

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "AdapterRunSpec":
        return cls(
            files=[str(item) for item in value.get("files", [])],
            filelists=[str(item) for item in value.get("filelists", [])],
            include_dirs=[str(item) for item in value.get("include_dirs", [])],
            defines=[str(item) for item in value.get("defines", [])],
            top_module=str(value["top_module"]) if value.get("top_module") else None,
            work_library=str(value["work_library"]) if value.get("work_library") else None,
            standard=str(value.get("standard", "systemverilog")),
            timeout_seconds=int(value["timeout_seconds"]) if value.get("timeout_seconds") is not None else None,
            extra_args=[str(item) for item in value.get("extra_args", [])],
            env={str(key): str(inner) for key, inner in dict(value.get("env", {})).items()},
        )

    def expanded(self, project_root: Path) -> "AdapterRunSpec":
        files = list(self.files)
        include_dirs = list(self.include_dirs)
        defines = list(self.defines)
        for filelist in self.filelists:
            parsed = _parse_filelist(project_root, filelist)
            files.extend(parsed["files"])
            include_dirs.extend(parsed["include_dirs"])
            defines.extend(parsed["defines"])
        return AdapterRunSpec(
            files=unique_preserve_order([item for item in files if str(item).strip()]),
            filelists=unique_preserve_order([item for item in self.filelists if str(item).strip()]),
            include_dirs=unique_preserve_order([item for item in include_dirs if str(item).strip()]),
            defines=unique_preserve_order([item for item in defines if str(item).strip()]),
            top_module=self.top_module,
            work_library=self.work_library,
            standard=self.standard,
            timeout_seconds=self.timeout_seconds,
            extra_args=list(self.extra_args),
            env=dict(self.env),
        )

    def summary(self, project_root: Path) -> dict[str, Any]:
        expanded = self.expanded(project_root)
        return {
            "files": expanded.files,
            "filelists": expanded.filelists,
            "include_dirs": expanded.include_dirs,
            "defines": expanded.defines,
            "top_module": expanded.top_module,
            "work_library": expanded.work_library,
            "standard": expanded.standard,
            "timeout_seconds": expanded.timeout_seconds,
            "extra_args": expanded.extra_args,
            "env": _redacted_env_summary(expanded.env),
        }


def _parse_filelist(project_root: Path, filelist: str) -> dict[str, list[str]]:
    path = Path(filelist)
    resolved = path if path.is_absolute() else project_root / path
    if not resolved.exists() or not resolved.is_file():
        raise AdapterExecutionError(f"filelist does not exist: {filelist}")
    files: list[str] = []
    include_dirs: list[str] = []
    defines: list[str] = []
    base = resolved.parent
    for raw_line in resolved.read_text(encoding="utf-8").splitlines():
        line = _strip_filelist_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("+incdir+"):
            include_dirs.extend(part for part in line.split("+")[2:] if part)
            continue
        if line.startswith("+define+"):
            defines.extend(part for part in line.split("+")[2:] if part)
            continue
        entry = Path(line)
        if not entry.is_absolute() and not (project_root / entry).exists() and (base / entry).exists():
            try:
                line = str((base / entry).resolve().relative_to(project_root.resolve())).replace("\\", "/")
            except ValueError:
                line = str((base / entry).resolve())
        files.append(line)
    return {"files": files, "include_dirs": include_dirs, "defines": defines}


def _strip_filelist_comment(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("#") or stripped.startswith("//"):
        return ""
    for marker in (" #", "\t#", " //", "\t//"):
        index = line.find(marker)
        if index >= 0:
            return line[:index]
    return line


def _redacted_env_summary(env: dict[str, str]) -> dict[str, str]:
    summary: dict[str, str] = {}
    for key, value in env.items():
        summary[key] = "<redacted>" if SECRET_KEY_RE.search(str(key)) else str(value)
    return summary


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
    setup_guidance: tuple[str, ...] = ()

    def is_available(self) -> bool:
        required = self.required_binaries or self.binary_names
        if not required:
            return True
        return all(executable_status(binary)[0] for binary in required)

    def version(self) -> str:
        binary = next(iter(self.required_binaries or self.binary_names), "")
        if not binary:
            return "unavailable"
        available, version, _ = executable_status(binary)
        return version if available else "unavailable"

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        raise NotImplementedError

    def build_command_from_spec(self, project_root: Path, spec: AdapterRunSpec) -> list[str]:
        expanded = spec.expanded(project_root)
        return self.build_command(project_root, expanded.files, expanded.extra_args)

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

    def missing_binaries(self) -> list[str]:
        required = self.required_binaries or self.binary_names
        missing: list[str] = []
        for binary in required:
            available, _, reason = executable_status(binary)
            if not available:
                missing.append(binary if reason == "not found on PATH" else f"{binary} ({reason})")
        return missing

    def setup_diagnostics(self, missing: list[str] | None = None) -> list[str]:
        missing = list(missing if missing is not None else self.missing_binaries())
        missing_text = ", ".join(missing) if missing else self.name
        diagnostics = [f"Install/configure {self.name} and ensure these binaries are on PATH: {missing_text}"]
        diagnostics.extend(self.setup_guidance)
        if self.binary_names:
            diagnostics.append(f"Verify with: {next(iter(self.binary_names))} --version")
        return diagnostics

    def unavailable_message(self) -> str:
        missing = self.missing_binaries()
        missing_text = ", ".join(missing) or ", ".join(self.required_binaries or self.binary_names) or self.name
        diagnostics = "; ".join(self.setup_diagnostics(missing))
        return f"{self.name} is not available in this host environment; unavailable required binaries: {missing_text}. {diagnostics}"

    def run(
        self,
        run_id: str,
        project_root: Path,
        files: list[str],
        artifacts_dir: Path,
        extra_args: list[str] | None = None,
        spec: AdapterRunSpec | None = None,
    ) -> AdapterExecution:
        run_spec = (spec or AdapterRunSpec.from_legacy(files, extra_args)).expanded(project_root)
        command = self.build_command_from_spec(project_root, run_spec)
        if not run_spec.files:
            raise AdapterExecutionError(f"{self.name} requires at least one input file")
        if not self.is_available():
            raise AdapterExecutionError(self.unavailable_message())
        started_at = utc_now()
        try:
            run_kwargs: dict[str, Any] = {"cwd": project_root, "capture_output": True, "text": True, "check": False}
            if run_spec.timeout_seconds is not None:
                run_kwargs["timeout"] = run_spec.timeout_seconds
            if run_spec.env:
                run_kwargs["env"] = {**dict(os.environ), **run_spec.env}
            process = subprocess.run(
                command,
                **run_kwargs,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdapterExecutionError(f"{self.name} timed out after {run_spec.timeout_seconds} second(s)") from exc
        except OSError as exc:
            raise AdapterExecutionError(f"failed to execute {self.name}: {exc}") from exc
        finished_at = utc_now()
        ensure_directory(artifacts_dir)
        log_path = artifacts_dir / f"{run_id}.log"
        combined = process.stdout + process.stderr
        log_path.write_text(combined, encoding="utf-8")
        observations = self.parse_output(run_id, combined)
        result = self.parse_result(project_root, run_spec.files, process.stdout, process.stderr, combined)
        result.setdefault("status", "passed" if process.returncode == 0 else "failed")
        result.setdefault("validation_mode", self.validation_mode)
        result["command"] = command
        result["cwd"] = str(project_root)
        result["adapter"] = {"name": self.name, "version": self.version(), "kind": self.kind}
        result["run_spec"] = run_spec.summary(project_root)
        artifacts = {"log_path": str(log_path)}
        artifacts.update(self.collect_artifacts(project_root, run_spec.files, artifacts_dir, run_id, process.stdout, process.stderr, combined))
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
