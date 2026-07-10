from __future__ import annotations

from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from telchines.adapters.base import AdapterExecution, AdapterRunSpec, ToolAdapter
from telchines.adapters.open_tools import SymbiYosysAdapter
from telchines.config import ProjectConfig
from telchines.models import SvaCandidate
from telchines.workflows import gen_sva


class MissingFormalAdapter(ToolAdapter):
    name = "symbiyosys"
    kind = "formal"
    category = "formal"
    supported_workflows = ("formal_validation",)

    def is_available(self) -> bool:
        return False

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["sby", *(extra_args or []), *files]


class PassingFormalAdapter(MissingFormalAdapter):
    def is_available(self) -> bool:
        return True

    def run(self, run_id, project_root, files, artifacts_dir, extra_args=None, spec=None):  # noqa: ANN001
        log_path = artifacts_dir / f"{run_id}.log"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text("summary: passed\np_start_seen proved\n", encoding="utf-8")
        return AdapterExecution(
            command=["sby", *files],
            cwd=str(project_root),
            exit_code=0,
            stdout="summary: passed\np_start_seen proved\n",
            stderr="",
            log_path=str(log_path),
            started_at="2026-07-04T00:00:00+00:00",
            finished_at="2026-07-04T00:00:01+00:00",
            observations=[],
            summary="symbiyosys exited with code 0",
            artifacts={"log_path": str(log_path)},
            result={"status": "passed", "validation_mode": "formal_run", "run_spec": spec.summary(project_root) if spec else {}},
        )


class RecordingAdapterValidationAdapter(ToolAdapter):
    name = "slang"
    kind = "simulator"
    category = "simulation"
    validation_mode = "compile_only"
    supported_workflows = ("generation_validation",)

    def is_available(self) -> bool:
        return True

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["slang", "--lint-only", *(extra_args or []), *files]

    def run(self, run_id, project_root, files, artifacts_dir, extra_args=None, spec=None):  # noqa: ANN001
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        log_path = artifacts_dir / f"{run_id}.log"
        log_path.write_text("slang: validation passed\n", encoding="utf-8")
        return AdapterExecution(
            command=["slang", "--lint-only", *files],
            cwd=str(project_root),
            exit_code=0,
            stdout="slang: validation passed\n",
            stderr="",
            log_path=str(log_path),
            started_at="2026-07-04T00:00:00+00:00",
            finished_at="2026-07-04T00:00:01+00:00",
            observations=[],
            summary="slang exited with code 0",
            artifacts={"log_path": str(log_path)},
            result={"status": "passed", "validation_mode": "compile_only"},
        )


class MatrixFormalAdapter(ToolAdapter):
    name = "symbiyosys"
    kind = "formal"
    category = "formal"

    def __init__(self, *, available: bool, supported: bool, exit_code: int) -> None:
        self._available = available
        self.supported_workflows = ("formal_validation",) if supported else ()
        self.exit_code = exit_code

    def is_available(self) -> bool:
        return self._available

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["sby", *(extra_args or []), *files]

    def run(self, run_id, project_root, files, artifacts_dir, extra_args=None, spec=None):  # noqa: ANN001
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        log_path = artifacts_dir / f"{run_id}.log"
        status = "passed" if self.exit_code == 0 else "failed"
        log_path.write_text(f"summary: {status}\n", encoding="utf-8")
        return AdapterExecution(
            command=["sby", *files],
            cwd=str(project_root),
            exit_code=self.exit_code,
            stdout=f"summary: {status}\n",
            stderr="",
            log_path=str(log_path),
            started_at="2026-07-04T00:00:00+00:00",
            finished_at="2026-07-04T00:00:01+00:00",
            observations=[],
            summary=f"symbiyosys exited with code {self.exit_code}",
            artifacts={"log_path": str(log_path)},
            result={"status": status, "validation_mode": "formal_run"},
        )


class FakeRegistry:
    def __init__(self, adapter: ToolAdapter) -> None:
        self.adapter = adapter

    def get(self, name: str) -> ToolAdapter:
        assert name == "symbiyosys"
        return self.adapter


class MultiRegistry:
    def __init__(self, adapters: dict[str, ToolAdapter]) -> None:
        self.adapters = adapters

    def get(self, name: str) -> ToolAdapter:
        return self.adapters[name]


class RaisingRegistry:
    def get(self, name: str) -> ToolAdapter:
        raise KeyError(name)


def _candidate() -> SvaCandidate:
    return SvaCandidate(
        candidate_id="sva_formal",
        task_id="task_formal",
        spec_path="docs/spec.md",
        rtl_path="rtl/dut.sv",
        file_path="dut_assertions.sv",
        candidate_content=(
            "module dut_assertions(input logic clk, input logic start_seen);\n"
            "property p_start_seen;\n"
            "  @(posedge clk) start_seen |-> start_seen;\n"
            "endproperty\n"
            "assert property (p_start_seen);\n"
            "endmodule\n"
            "bind dut dut_assertions dut_assertions_i(.clk(clk), .start_seen(start_seen));\n"
        ),
        explanation="formal test",
        status="proposed",
    )


def _write_project(root: Path) -> ProjectConfig:
    (root / "rtl").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "spec.md").write_text("spec\n", encoding="utf-8")
    (root / "rtl" / "dut.sv").write_text(
        "module dut(input logic clk, output logic start_seen);\nendmodule\n",
        encoding="utf-8",
    )
    (root / "dut_assertions.sv").write_text(_candidate().candidate_content, encoding="utf-8")
    config = ProjectConfig.init_project(root)
    config.generation["sva"]["validation_adapters"] = []
    config.generation["sva"]["formal"]["mode"] = "required"
    config.save()
    return config


def test_required_formal_validation_fails_when_adapter_missing(work_root: Path, monkeypatch) -> None:
    config = _write_project(work_root)
    monkeypatch.setattr(gen_sva, "AdapterRegistry", lambda: FakeRegistry(MissingFormalAdapter()))

    validator, _, returncode, combined, tool_result = gen_sva._run_validation(config, work_root, _candidate())

    assert validator == "symbiyosys"
    assert returncode == 1
    assert "not available" in combined
    assert tool_result["formal_status"] == "failed"


def test_required_formal_validation_reports_symbiyosys_setup_guidance(work_root: Path, monkeypatch) -> None:
    config = _write_project(work_root)
    monkeypatch.setattr("telchines.adapters.base.shutil.which", lambda _: None)
    monkeypatch.setattr(gen_sva, "AdapterRegistry", lambda: FakeRegistry(SymbiYosysAdapter()))

    validator, _, returncode, combined, tool_result = gen_sva._run_validation(config, work_root, _candidate())

    assert validator == "symbiyosys"
    assert returncode == 1
    assert "sby" in combined
    assert tool_result["formal_status"] == "failed"
    assert any("OSS CAD Suite" in item for item in tool_result["setup_diagnostics"])


def test_formal_validation_records_sby_artifact(work_root: Path, monkeypatch) -> None:
    config = _write_project(work_root)
    monkeypatch.setattr(gen_sva, "AdapterRegistry", lambda: FakeRegistry(PassingFormalAdapter()))

    validator, command, returncode, _, tool_result = gen_sva._run_validation(config, work_root, _candidate())

    assert validator == "symbiyosys"
    assert command[0] == "sby"
    assert returncode == 0
    assert tool_result["validation_mode"] == "formal_run"
    assert tool_result["formal_status"] == "passed"
    assert Path(str(tool_result["command_artifacts"]["sby_file"])).exists()


def test_sva_adapter_validation_records_command_artifacts(work_root: Path, monkeypatch) -> None:
    config = _write_project(work_root)
    config.generation["sva"]["validation_adapters"] = ["slang"]
    config.generation["sva"]["formal"]["mode"] = "off"
    config.adapters = ["slang"]
    config.save()
    monkeypatch.setattr(gen_sva, "AdapterRegistry", lambda: MultiRegistry({"slang": RecordingAdapterValidationAdapter()}))

    validator, command, returncode, _, tool_result = gen_sva._run_validation(config, work_root, _candidate())

    assert validator == "slang"
    assert command[0] == "slang"
    assert returncode == 0
    assert tool_result["validation_mode"] == "adapter_backed"
    assert Path(str(tool_result["command_artifacts"]["log_path"])).exists()


@given(
    mode=st.sampled_from(["off", "auto", "required"]),
    registered=st.booleans(),
    enabled=st.booleans(),
    available=st.booleans(),
    supported=st.booleans(),
    exit_code=st.sampled_from([0, 1]),
)
@settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_formal_validation_mode_and_availability_matrix(
    work_root: Path,
    monkeypatch,
    mode: str,
    registered: bool,
    enabled: bool,
    available: bool,
    supported: bool,
    exit_code: int,
) -> None:
    config = _write_project(work_root)
    config.generation["sva"]["formal"]["mode"] = mode
    config.adapters = ["symbiyosys"] if enabled else ["verilator"]
    config.save()
    adapter = MatrixFormalAdapter(available=available, supported=supported, exit_code=exit_code)
    monkeypatch.setattr(gen_sva, "AdapterRegistry", (lambda: FakeRegistry(adapter)) if registered else RaisingRegistry)

    validator, _, returncode, _, tool_result = gen_sva._run_validation(config, work_root, _candidate())

    if mode == "off":
        assert validator == "builtin_sva_syntax"
        assert returncode == 0
        assert tool_result["formal_status"] == "not_run"
        return

    setup_ready = registered and enabled and available and supported
    if setup_ready and (mode == "required" or exit_code == 0):
        assert validator == "symbiyosys"
        assert returncode == exit_code
        assert tool_result["formal_status"] == ("passed" if exit_code == 0 else "failed")
        assert tool_result["validation_mode"] == "formal_run"
        assert tool_result["command_artifacts"]["sby_file"]
    elif setup_ready:
        assert validator == "builtin_sva_syntax"
        assert returncode == 0
        assert tool_result["formal_status"] == "failed"
        assert tool_result["command_artifacts"]["sby_file"]
    elif mode == "required":
        assert validator == "symbiyosys"
        assert returncode == 1
        assert tool_result["formal_status"] == "failed"
        assert tool_result["setup_diagnostics"]
    else:
        assert validator == "builtin_sva_syntax"
        assert returncode == 0
        assert tool_result["formal_status"] == "skipped"
        assert tool_result["setup_diagnostics"]


def test_formal_sby_file_uses_run_spec_top_and_sources(work_root: Path, monkeypatch) -> None:
    config = _write_project(work_root)
    monkeypatch.setattr(gen_sva, "AdapterRegistry", lambda: FakeRegistry(PassingFormalAdapter()))

    _, _, returncode, _, tool_result = gen_sva._run_validation(
        config,
        work_root,
        _candidate(),
        run_spec=AdapterRunSpec(
            files=["rtl/dut.sv", "rtl/helper.sv"],
            include_dirs=["rtl/include"],
            defines=["FORMAL=1"],
            top_module="formal_top",
            work_library="work",
            extra_args=["--append"],
            env={"API_TOKEN": "secret"},
        ),
    )

    sby_text = Path(str(tool_result["command_artifacts"]["sby_file"])).read_text(encoding="utf-8")
    assert returncode == 0
    assert "mode bmc" in sby_text
    assert "depth 4" in sby_text
    assert "read -formal -sv -Irtl/include -DFORMAL=1 dut.sv" in sby_text
    assert "read -formal -sv -Irtl/include -DFORMAL=1 helper.sv" in sby_text
    assert "read -formal -sv -Irtl/include -DFORMAL=1 dut_assertions.sv" in sby_text
    assert "prep -top formal_top" in sby_text
    run_spec = tool_result["adapter_result"]["run_spec"]
    assert run_spec["work_library"] == "work"
    assert run_spec["env"]["API_TOKEN"] == "<redacted>"
