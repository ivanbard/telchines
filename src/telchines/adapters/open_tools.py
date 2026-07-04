from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from telchines.adapters.base import AdapterExecution, AdapterRunSpec, ToolAdapter
from telchines.errors import AdapterExecutionError
from telchines.utils import ensure_directory, utc_now


class VerilatorAdapter(ToolAdapter):
    name = "verilator"
    kind = "simulator"
    category = "simulation"
    binary_names = ("verilator",)
    supported_workflows = ("repair_validation", "generation_validation")
    artifact_types = ("log", "stdout", "stderr")

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["verilator", "--lint-only", *(extra_args or []), *files]

    def build_command_from_spec(self, project_root: Path, spec: AdapterRunSpec) -> list[str]:
        spec = spec.expanded(project_root)
        command = ["verilator", "--lint-only"]
        if spec.standard.lower() in {"systemverilog", "sv", "2012", "sv2012"}:
            command.append("-sv")
        command.extend(f"-I{path}" for path in spec.include_dirs)
        command.extend(f"-D{define}" for define in spec.defines)
        if spec.top_module:
            command.extend(["--top-module", spec.top_module])
        command.extend(spec.extra_args)
        command.extend(spec.files)
        return command


class IcarusAdapter(ToolAdapter):
    name = "iverilog"
    kind = "simulator"
    category = "simulation"
    validation_mode = "compile_and_run"
    binary_names = ("iverilog", "vvp")
    required_binaries = ("iverilog", "vvp")
    supported_workflows = ("repair_validation",)
    artifact_types = ("log", "stdout", "stderr", "executable")

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["iverilog", "-g2012", *(extra_args or []), *files]

    def build_command_from_spec(self, project_root: Path, spec: AdapterRunSpec) -> list[str]:
        spec = spec.expanded(project_root)
        command = ["iverilog", _icarus_standard_flag(spec.standard)]
        command.extend(f"-I{path}" for path in spec.include_dirs)
        command.extend(f"-D{define}" for define in spec.defines)
        if spec.top_module:
            command.extend(["-s", spec.top_module])
        command.extend(spec.extra_args)
        command.extend(spec.files)
        return command

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
        if not run_spec.files:
            raise AdapterExecutionError(f"{self.name} requires at least one input file")
        if not self.is_available():
            binaries = ", ".join(self.required_binaries or self.binary_names) or self.name
            raise AdapterExecutionError(f"{self.name} is not available on PATH; expected: {binaries}")

        ensure_directory(artifacts_dir)
        executable_path = artifacts_dir / f"{run_id}.out"
        compile_command = self.build_command_from_spec(project_root, run_spec)
        compile_command[2:2] = ["-o", str(executable_path)]
        compile_started_at = utc_now()
        try:
            run_kwargs = {"cwd": project_root, "capture_output": True, "text": True, "check": False}
            if run_spec.timeout_seconds is not None:
                run_kwargs["timeout"] = run_spec.timeout_seconds
            compile_process = subprocess.run(compile_command, **run_kwargs)
        except subprocess.TimeoutExpired as exc:
            raise AdapterExecutionError(f"{self.name} compile step timed out after {run_spec.timeout_seconds} second(s)") from exc
        except OSError as exc:
            raise AdapterExecutionError(f"failed to execute {self.name} compile step: {exc}") from exc

        run_command = ["vvp", str(executable_path)]
        run_stdout = ""
        run_stderr = ""
        run_exit_code: int | None = None
        if compile_process.returncode == 0:
            try:
                run_process = subprocess.run(run_command, **run_kwargs)
            except subprocess.TimeoutExpired as exc:
                raise AdapterExecutionError(f"{self.name} run step timed out after {run_spec.timeout_seconds} second(s)") from exc
            except OSError as exc:
                raise AdapterExecutionError(f"failed to execute {self.name} run step: {exc}") from exc
            run_stdout = run_process.stdout
            run_stderr = run_process.stderr
            run_exit_code = run_process.returncode

        finished_at = utc_now()
        combined = compile_process.stdout + compile_process.stderr + run_stdout + run_stderr
        log_path = artifacts_dir / f"{run_id}.log"
        log_path.write_text(combined, encoding="utf-8")
        observations = self.parse_output(run_id, combined)
        overall_exit_code = compile_process.returncode if compile_process.returncode != 0 else (run_exit_code or 0)
        result = {
            "status": "passed" if overall_exit_code == 0 else "failed",
            "validation_mode": self.validation_mode,
            "compile_command": compile_command,
            "run_command": run_command if compile_process.returncode == 0 else [],
            "compile_exit_code": compile_process.returncode,
            "run_exit_code": run_exit_code,
            "artifact_path": str(executable_path),
            "command": compile_command,
            "cwd": str(project_root),
            "adapter": {"name": self.name, "version": self.version(), "kind": self.kind},
            "run_spec": run_spec.summary(project_root),
        }
        summary = f"{self.name} compile+run exited with code {overall_exit_code}"
        if observations:
            summary = f"{summary}; first observation: {observations[0].signature}"
        return AdapterExecution(
            command=compile_command,
            cwd=str(project_root),
            exit_code=overall_exit_code,
            stdout=compile_process.stdout + run_stdout,
            stderr=compile_process.stderr + run_stderr,
            log_path=str(log_path),
            started_at=compile_started_at,
            finished_at=finished_at,
            observations=observations,
            summary=summary,
            artifacts={"log_path": str(log_path), "compiled_executable": str(executable_path)},
            result=result,
        )


class SlangAdapter(ToolAdapter):
    name = "slang"
    kind = "simulator"
    category = "simulation"
    binary_names = ("slang",)
    supported_workflows = ("repair_validation", "generation_validation")
    artifact_types = ("log", "stdout", "stderr")

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["slang", "--lint-only", *(extra_args or []), *files]

    def build_command_from_spec(self, project_root: Path, spec: AdapterRunSpec) -> list[str]:
        spec = spec.expanded(project_root)
        command = ["slang", "--lint-only"]
        command.extend(f"-I{path}" for path in spec.include_dirs)
        command.extend(f"-D{define}" for define in spec.defines)
        if spec.top_module:
            command.extend(["--top", spec.top_module])
        command.extend(spec.extra_args)
        command.extend(spec.files)
        return command


class VeribleAdapter(ToolAdapter):
    name = "verible"
    kind = "linter"
    category = "lint"
    binary_names = ("verible-verilog-lint",)
    supported_workflows = ("repair_validation",)
    artifact_types = ("log", "stdout", "stderr")

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["verible-verilog-lint", *(extra_args or []), *files]

    def build_command_from_spec(self, project_root: Path, spec: AdapterRunSpec) -> list[str]:
        spec = spec.expanded(project_root)
        return ["verible-verilog-lint", *spec.extra_args, *spec.files]


class SymbiYosysAdapter(ToolAdapter):
    name = "symbiyosys"
    kind = "formal"
    category = "formal"
    binary_names = ("sby",)
    supported_workflows = ("formal_validation",)
    artifact_types = ("log", "report", "counterexample")

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["sby", *(extra_args or []), *files]

    def build_command_from_spec(self, project_root: Path, spec: AdapterRunSpec) -> list[str]:
        spec = spec.expanded(project_root)
        return ["sby", *spec.extra_args, *spec.files]

    def parse_result(self, project_root: Path, files: list[str], stdout: str, stderr: str, combined: str) -> dict[str, Any]:
        return _parse_symbiyosys_result(project_root, combined)


def _parse_symbiyosys_result(project_root: Path, combined: str) -> dict[str, Any]:
    lowered = combined.lower()
    status = "unknown"
    if re.search(r"\b(status|summary)\s*:\s*passed\b", lowered):
        status = "passed"
    elif re.search(r"\b(status|summary)\s*:\s*failed\b", lowered):
        status = "failed"
    elif "assert failed" in lowered or "counterexample" in lowered:
        status = "failed"

    property_ids = sorted(
        {
            match.group("name")
            for match in re.finditer(
                r"(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s+(?:proved|failed|covered|unreached)",
                combined,
                flags=re.IGNORECASE,
            )
        }
    )
    referenced_artifacts = _existing_artifacts(project_root, combined)
    counterexample_paths = [path for path in referenced_artifacts if path.endswith((".vcd", ".fst"))]
    report_paths = [path for path in referenced_artifacts if path.endswith((".json", ".txt", ".log"))]
    return {
        "status": status,
        "validation_mode": "formal_run",
        "property_ids": property_ids,
        "counterexample_paths": counterexample_paths,
        "report_paths": report_paths,
        "referenced_artifacts": referenced_artifacts,
    }


def _icarus_standard_flag(standard: str) -> str:
    normalized = standard.lower().replace("-", "")
    if normalized in {"verilog2005", "2005", "v2005"}:
        return "-g2005"
    if normalized in {"verilog2001", "2001", "v2001"}:
        return "-g2001"
    return "-g2012"


def _existing_artifacts(project_root: Path, combined: str) -> list[str]:
    candidates = re.findall(r"([A-Za-z0-9_./\\\\-]+\.(?:vcd|fst|json|txt|log))", combined)
    artifacts: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        path = Path(candidate)
        resolved = path if path.is_absolute() else (project_root / path)
        try:
            normalized = str(resolved.resolve().relative_to(project_root.resolve())).replace("\\", "/")
        except ValueError:
            normalized = str(resolved.resolve()).replace("\\", "/")
        if not resolved.exists() or normalized in seen:
            continue
        seen.add(normalized)
        artifacts.append(normalized)
    return artifacts
