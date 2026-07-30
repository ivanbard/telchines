from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import importlib.util
from pathlib import Path
from typing import Any

from telchines.adapters.base import AdapterExecution, AdapterRunSpec, ToolAdapter, executable_status
from telchines.errors import AdapterExecutionError
from telchines.utils import ensure_directory, utc_now


class VerilatorAdapter(ToolAdapter):
    name = "verilator"
    kind = "simulator"
    category = "simulation"
    binary_names = ("verilator",)
    supported_workflows = ("repair_validation", "generation_validation")
    artifact_types = ("log", "stdout", "stderr")
    setup_guidance = (
        "Windows/MSYS2 UCRT64: pacman -S mingw-w64-ucrt-x86_64-verilator, then add C:\\msys64\\ucrt64\\bin to PATH.",
        "Linux: use your distro package, for example sudo apt-get install verilator on Debian/Ubuntu.",
        "WSL: install Verilator inside the distro and run Telchines from that same shell.",
    )

    def is_available(self) -> bool:
        binary = _verilator_binary()
        return bool(binary and executable_status(binary)[0])

    def missing_binaries(self) -> list[str]:
        binary = _verilator_binary()
        if not binary:
            return ["verilator or verilator_bin.exe"]
        available, _, reason = executable_status(binary)
        return [] if available else [f"verilator or verilator_bin.exe ({reason})"]

    def version(self) -> str:
        binary = _verilator_binary()
        if not binary:
            return "unavailable"
        available, version, _ = executable_status(binary)
        return version if available else "unavailable"

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return _verilator_command(project_root, ["--lint-only", *(extra_args or []), *files])

    def build_command_from_spec(self, project_root: Path, spec: AdapterRunSpec) -> list[str]:
        spec = spec.expanded(project_root)
        args = ["--lint-only"]
        if spec.standard.lower() in {"systemverilog", "sv", "2012", "sv2012"}:
            args.append("-sv")
        args.extend(f"-I{path}" for path in spec.include_dirs)
        args.extend(f"-D{define}" for define in spec.defines)
        if spec.top_module:
            args.extend(["--top-module", spec.top_module])
        args.extend(spec.extra_args)
        args.extend(spec.files)
        return _verilator_command(project_root, args)


class IcarusAdapter(ToolAdapter):
    name = "iverilog"
    kind = "simulator"
    category = "simulation"
    validation_mode = "compile_and_run"
    binary_names = ("iverilog", "vvp")
    required_binaries = ("iverilog", "vvp")
    supported_workflows = ("repair_validation",)
    artifact_types = ("log", "stdout", "stderr", "executable")
    setup_guidance = (
        "Windows/MSYS2 UCRT64: pacman -S mingw-w64-ucrt-x86_64-iverilog, then add C:\\msys64\\ucrt64\\bin to PATH.",
        "Linux: use your distro package, for example sudo apt-get install iverilog on Debian/Ubuntu.",
    )

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
            raise AdapterExecutionError(self.unavailable_message())

        ensure_directory(artifacts_dir)
        executable_path = artifacts_dir / f"{run_id}.out"
        compile_command = self.build_command_from_spec(project_root, run_spec)
        compile_command[2:2] = ["-o", str(executable_path)]
        compile_started_at = utc_now()
        try:
            run_kwargs = {"cwd": project_root, "capture_output": True, "text": True, "check": False}
            if run_spec.timeout_seconds is not None:
                run_kwargs["timeout"] = run_spec.timeout_seconds
            if run_spec.env:
                run_kwargs["env"] = {**dict(os.environ), **run_spec.env}
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
    setup_guidance = (
        "Windows/Linux/macOS: download a prebuilt slang CLI from https://github.com/MikePopoloski/slang/releases or build from source.",
        "Python fallback: install pyslang with python -m pip install pyslang when the slang CLI is not available.",
    )

    def is_available(self) -> bool:
        return executable_status("slang")[0] or _pyslang_available()

    def missing_binaries(self) -> list[str]:
        return [] if self.is_available() else ["slang or pyslang"]

    def version(self) -> str:
        if executable_status("slang")[0]:
            return super().version()
        if not _pyslang_available():
            return "unavailable"
        try:
            import pyslang
        except ImportError:
            return "unavailable"
        return f"pyslang {pyslang.VersionInfo.getVersionString()}"

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return _slang_command(["--lint-only", *(extra_args or []), *files])

    def build_command_from_spec(self, project_root: Path, spec: AdapterRunSpec) -> list[str]:
        spec = spec.expanded(project_root)
        args = ["--lint-only"]
        args.extend(f"-I{path}" for path in spec.include_dirs)
        args.extend(f"-D{define}" for define in spec.defines)
        if spec.top_module:
            args.extend(["--top", spec.top_module])
        args.extend(spec.extra_args)
        args.extend(spec.files)
        return _slang_command(args)


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
    setup_guidance = (
        "Windows/Linux/macOS: install the free OSS CAD Suite from https://github.com/YosysHQ/oss-cad-suite-build/releases and activate its environment.",
        "Linux source install: install Yosys and solvers, then build/install sby from https://github.com/YosysHQ/sby.",
        "Windows shell: run oss-cad-suite\\environment.bat or start.bat before running Telchines so sby is on PATH.",
    )

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return _symbiyosys_command(project_root, [*(extra_args or []), *files])

    def build_command_from_spec(self, project_root: Path, spec: AdapterRunSpec) -> list[str]:
        spec = spec.expanded(project_root)
        return _symbiyosys_command(project_root, [*spec.extra_args, *spec.files])

    def parse_result(self, project_root: Path, files: list[str], stdout: str, stderr: str, combined: str) -> dict[str, Any]:
        return _parse_symbiyosys_result(project_root, combined)


def _verilator_binary() -> str:
    for binary in ("verilator", "verilator.exe", "verilator_bin", "verilator_bin.exe"):
        found = shutil.which(binary)
        if found:
            return binary
    return ""


def _slang_command(args: list[str]) -> list[str]:
    if executable_status("slang")[0]:
        return ["slang", *args]
    if _pyslang_available():
        return [sys.executable, "-m", "telchines.adapters.pyslang_runner", *args]
    return ["slang", *args]


def _pyslang_available() -> bool:
    return importlib.util.find_spec("pyslang") is not None


def _symbiyosys_command(project_root: Path, args: list[str]) -> list[str]:
    command = ["sby"]
    if project_root.is_absolute():
        smtbmc_override = _yosys_smtbmc_python_override()
        if smtbmc_override:
            command.extend(["--smtbmc", smtbmc_override])
    command.extend(args)
    return command


def _yosys_smtbmc_python_override() -> str:
    smtbmc = shutil.which("yosys-smtbmc")
    if not smtbmc:
        return ""
    smtbmc_path = Path(smtbmc)
    script = smtbmc_path.with_name("yosys-smtbmc-script.py")
    python = smtbmc_path.with_name("python3.exe")
    if os.name == "nt" and script.exists() and python.exists():
        return f"{python.as_posix()} {script.as_posix()}"
    return ""


def _verilator_command(project_root: Path, args: list[str]) -> list[str]:
    if not project_root.is_absolute():
        return ["verilator", *args]
    if shutil.which("verilator"):
        return ["verilator", *args]
    wrapper = _extensionless_verilator_wrapper()
    bash = _msys_bash(wrapper) if wrapper else ""
    if wrapper and bash:
        project = _msys_path(project_root.resolve())
        command = "export PATH=/ucrt64/bin:/usr/bin:/bin"
        if project:
            command += f"; cd {shlex.quote(project)}"
        command += "; verilator " + " ".join(shlex.quote(arg) for arg in args)
        return [bash, "-lc", command]
    return [_verilator_binary() or "verilator", *args]


def _extensionless_verilator_wrapper() -> Path | None:
    path_env = os.environ.get("PATH", "")
    for raw_entry in path_env.split(os.pathsep):
        if not raw_entry:
            continue
        candidate = Path(raw_entry) / "verilator"
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _msys_bash(wrapper: Path) -> str:
    msys_root = wrapper.parent.parent.parent
    candidate = msys_root / "usr" / "bin" / "bash.exe"
    if candidate.exists():
        return str(candidate)
    return shutil.which("bash") or ""


def _msys_path(path: Path) -> str:
    cygpath = shutil.which("cygpath")
    if cygpath is None:
        return ""
    try:
        result = subprocess.run([cygpath, "-u", str(path)], capture_output=True, text=True, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


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
