from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from telchines.adapters.base import AdapterRunSpec
from telchines.config import ProjectConfig
from telchines.models import CocotbCandidate
from telchines.run_store import RunStore
from telchines.workflows import gen_cocotb


def _candidate(content: str | None = None) -> CocotbCandidate:
    return CocotbCandidate(
        candidate_id="cocotb_validation",
        task_id="task_cocotb_validation",
        dut_path="rtl/uart_rx.sv",
        spec_path=None,
        top_module="uart_rx",
        file_path=".tel/artifacts/generated/cocotb/test_uart_rx.py",
        manifest_path=".tel/artifacts/generated/cocotb/uart_rx_cocotb_manifest.json",
        candidate_content=content
        if content is not None
        else "import cocotb\n\n@cocotb.test()\nasync def test_uart_rx(dut):\n    pass\n",
        explanation="test",
        status="proposed",
    )


def _config(sample_project: Path, mode: str = "auto") -> ProjectConfig:
    config = ProjectConfig.load(sample_project)
    config.generation["cocotb"]["executable_smoke"] = mode
    config.generation["cocotb"]["simulator"] = "icarus"
    config.save()
    return config


@pytest.mark.parametrize(
    ("mode", "simulator_available", "make_returncode", "expected_status", "expected_executable"),
    [
        ("off", True, 0, "passed", "skipped"),
        ("auto", False, 0, "passed", "skipped"),
        ("required", False, 0, "failed", "failed"),
        ("auto", True, 0, "passed", "passed"),
        ("required", True, 1, "failed", "failed"),
    ],
)
def test_cocotb_executable_smoke_mode_matrix(
    sample_project: Path,
    monkeypatch,
    mode: str,
    simulator_available: bool,
    make_returncode: int,
    expected_status: str,
    expected_executable: str,
) -> None:
    config = _config(sample_project, mode)
    store = RunStore(config)
    commands: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(command, **kwargs):  # noqa: ANN001
        commands.append(command)
        if command[0] == "make":
            return Result(make_returncode, "SMOKE\n" if make_returncode == 0 else "", "FAIL\n" if make_returncode else "")
        return Result(0, "", "")

    monkeypatch.setattr(gen_cocotb, "_select_cocotb_simulator", lambda preferred: ("icarus", []) if simulator_available else (None, ["no simulator"]))
    monkeypatch.setattr(gen_cocotb, "_cocotb_common_missing", lambda: [])
    monkeypatch.setattr(gen_cocotb, "_cocotb_makefiles_dir", lambda: (Path("/opt/cocotb/makefiles"), []))
    monkeypatch.setattr(gen_cocotb, "_cocotb_execution_contract", lambda simulator: ("supported", []))
    monkeypatch.setattr(gen_cocotb.subprocess, "run", fake_run)
    monkeypatch.setattr(gen_cocotb, "_run_cocotb_smoke_command", lambda command, **kwargs: fake_run(command, **kwargs))

    validation_run = gen_cocotb.validate_cocotb_candidate(config, store, _candidate())

    assert validation_run.status == expected_status
    assert validation_run.tool_result["executable_status"] == expected_executable
    if expected_executable == "passed":
        assert validation_run.tool_result["validation_mode"] == "compile_and_run"
    if simulator_available and mode != "off":
        assert any(command[0] == "make" for command in commands)
    else:
        assert not any(command[0] == "make" for command in commands)


def test_cocotb_smoke_makefile_records_compile_context(sample_project: Path, monkeypatch) -> None:
    config = _config(sample_project, "required")
    store = RunStore(config)
    calls: list[dict[str, object]] = []

    class Result:
        returncode = 0
        stdout = "smoke passed\n"
        stderr = ""

    def fake_run(command, **kwargs):  # noqa: ANN001
        calls.append({"command": command, **kwargs})
        return Result()

    monkeypatch.setattr(gen_cocotb, "_select_cocotb_simulator", lambda preferred: ("icarus", []))
    monkeypatch.setattr(gen_cocotb, "_cocotb_common_missing", lambda: [])
    monkeypatch.setattr(gen_cocotb, "_cocotb_makefiles_dir", lambda: (Path("/opt/cocotb/makefiles"), []))
    monkeypatch.setattr(gen_cocotb, "_cocotb_execution_contract", lambda simulator: ("supported", []))
    monkeypatch.setattr(gen_cocotb.shutil, "which", lambda name: "C:/msys64/usr/bin/make.exe" if name == "make" else None)
    monkeypatch.setattr(gen_cocotb.subprocess, "run", fake_run)
    monkeypatch.setattr(gen_cocotb, "_run_cocotb_smoke_command", lambda command, **kwargs: fake_run(command, **kwargs))

    validation_run = gen_cocotb.validate_cocotb_candidate(
        config,
        store,
        _candidate(),
        run_spec=AdapterRunSpec(
            files=["rtl/uart_rx.sv"],
            include_dirs=["rtl/include"],
            defines=["SIM=1"],
            top_module="uart_rx_tb_top",
            extra_args=["-Wall"],
            env={"API_KEY": "secret", "VISIBLE": "ok"},
        ),
    )

    makefile = Path(validation_run.tool_result["command_artifacts"]["cocotb_smoke_makefile"])
    log_path = Path(validation_run.tool_result["command_artifacts"]["cocotb_smoke_log"])
    makefile_text = makefile.read_text(encoding="utf-8")
    assert "TOPLEVEL = uart_rx_tb_top" in makefile_text
    assert "MODULE = test_uart_rx" in makefile_text
    assert "VERILOG_SOURCES" in makefile_text
    assert "COMPILE_ARGS += -Irtl/include -DSIM=1 -Wall" in makefile_text
    assert "cocotb/makefiles/Makefile.sim" in makefile_text
    assert log_path.read_text(encoding="utf-8") == "smoke passed\n\nsmoke passed\n"
    assert Path(validation_run.tool_result["command_artifacts"]["cocotb_compile_log"]).read_text(encoding="utf-8") == "smoke passed\n"
    assert Path(validation_run.tool_result["command_artifacts"]["cocotb_run_log"]).read_text(encoding="utf-8") == "smoke passed\n"
    make_call = next(call for call in calls if call["command"][0] == "make")
    assert any(argument.startswith("PYTHON_BIN=") for argument in make_call["command"])
    assert make_call["env"]["PATH"].startswith(str(Path("C:/msys64/usr/bin").resolve()))
    assert make_call["env"]["API_KEY"] == "secret"
    assert validation_run.tool_result["environment_summary"]["API_KEY"] == "<redacted>"
    assert validation_run.tool_result["environment_summary"]["VISIBLE"] == "ok"
    assert validation_run.tool_result["environment_summary"]["PYTHONPATH"]
    assert validation_run.tool_result["environment_summary"]["COCOTB_MAKEFILES"].replace("\\", "/").endswith("/opt/cocotb/makefiles")
    assert validation_run.tool_result["environment_summary"]["PYTHON_BIN"]


@given(has_import=st.booleans(), has_decorator=st.booleans())
@settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_cocotb_structural_failures_skip_executable_smoke(sample_project: Path, monkeypatch, has_import: bool, has_decorator: bool) -> None:
    config = _config(sample_project, "required")
    store = RunStore(config)
    calls: list[str] = []

    def fake_smoke(*args, **kwargs):  # noqa: ANN001
        calls.append("smoke")
        return gen_cocotb._smoke_skipped([])

    monkeypatch.setattr(gen_cocotb, "_run_executable_smoke", fake_smoke)
    content = "\n".join(
        [
            "import cocotb" if has_import else "# missing import",
            "@cocotb.test()" if has_decorator else "# missing decorator",
            "async def test_smoke(dut):",
            "    pass",
            "",
        ]
    )

    validation_run = gen_cocotb.validate_cocotb_candidate(config, store, _candidate(content))

    should_pass_structure = has_import and has_decorator
    assert bool(calls) is should_pass_structure
    assert validation_run.status == ("passed" if should_pass_structure else "failed")


def test_cocotb_python_syntax_failure_skips_executable_smoke(sample_project: Path, monkeypatch) -> None:
    config = _config(sample_project, "required")
    store = RunStore(config)
    monkeypatch.setattr(gen_cocotb, "_run_executable_smoke", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("smoke should not run")))

    validation_run = gen_cocotb.validate_cocotb_candidate(config, store, _candidate("import cocotb\n@cocotb.test()\ndef broken(:\n"))

    assert validation_run.status == "failed"
    assert validation_run.tool_result["executable_status"] == "skipped"
    assert validation_run.tool_result["stages"]["python_syntax"]["status"] == "failed"
    assert validation_run.tool_result["stages"]["simulator_compile"]["status"] == "skipped"


def test_cocotb_windows_embedding_failure_is_reported_as_init_failure(sample_project: Path) -> None:
    stages = gen_cocotb._smoke_stages_after_run(
        1,
        "C:\\iverilog\\bin\\vvp.exe\nUnexpected sys.executable value: got C:\\iverilog\\bin\\vvp.exe\nRuntimeError: No simulator available!\n",
        ["make", "results.xml"],
        sample_project / "missing_results.xml",
    )

    assert stages["simulator_launch"]["status"] == "passed"
    assert stages["cocotb_init"]["status"] == "failed"
    assert stages["test_results"]["status"] == "skipped"
    summary = gen_cocotb._validation_summary(1, "", stages=stages, executable_status="failed")
    assert summary.startswith("cocotb initialization failed:")
    assert "py_compile" not in summary


def test_cocotb_execution_contract_skips_missing_vvp(monkeypatch) -> None:
    monkeypatch.setattr(gen_cocotb.shutil, "which", lambda name: None if name == "vvp" else f"/usr/bin/{name}")
    monkeypatch.setattr(gen_cocotb, "_make_uses_msys", lambda: False)

    class Result:
        returncode = 0
        stdout = "/opt/cocotb/libs/cocotbvpi_icarus.vpl\n"
        stderr = ""

    monkeypatch.setattr(gen_cocotb.subprocess, "run", lambda *args, **kwargs: Result())
    status, diagnostics = gen_cocotb._cocotb_execution_contract("icarus")

    assert status == "unsupported"
    assert diagnostics == ["simulator launch prerequisite missing: vvp is not available on PATH"]


def test_cocotb_execution_contract_rejects_msys_make_with_native_icarus(monkeypatch) -> None:
    monkeypatch.setattr(gen_cocotb.os, "name", "nt")
    monkeypatch.setattr(gen_cocotb, "_make_uses_msys", lambda: True)
    monkeypatch.setattr(gen_cocotb.shutil, "which", lambda name: f"C:/tools/{name}.exe")

    class Result:
        returncode = 0
        stdout = "C:/python313/Lib/site-packages/cocotb/libs/cocotbvpi_icarus.vpl\n"
        stderr = ""

    monkeypatch.setattr(gen_cocotb.subprocess, "run", lambda *args, **kwargs: Result())
    status, diagnostics = gen_cocotb._cocotb_execution_contract("icarus")

    assert status == "unsupported"
    assert any("Windows/MSYS/native-Icarus" in diagnostic for diagnostic in diagnostics)


def test_cocotb_smoke_timeout_terminates_process_tree(monkeypatch, sample_project: Path) -> None:
    class Process:
        pid = 1234
        returncode = None
        calls = 0

        def communicate(self, timeout=None):  # noqa: ANN001
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["make"], timeout, output=b"compile output\n", stderr=b"compile error\n")
            self.returncode = -9
            return "", ""

    import subprocess

    process = Process()
    terminated: list[int] = []
    monkeypatch.setattr(gen_cocotb.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(gen_cocotb, "_terminate_cocotb_process_tree", lambda value: terminated.append(value.pid))

    result = gen_cocotb._run_cocotb_smoke_command(["make"], cwd=sample_project, env={}, timeout=7)

    assert result.returncode == 124
    assert terminated == [1234]
    assert "terminated the process tree" in result.stderr


def test_cocotb_makefiles_dir_falls_back_to_python_module(monkeypatch) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "/tmp/cocotb-makefiles\n"
        stderr = ""

    monkeypatch.setattr(gen_cocotb.shutil, "which", lambda name: None if name == "cocotb-config" else f"/usr/bin/{name}")
    monkeypatch.setattr(gen_cocotb.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(Path, "exists", lambda self: self.as_posix() == "/tmp/cocotb-makefiles/Makefile.sim")

    def fake_run(command, **kwargs):  # noqa: ANN001
        calls.append(command)
        return Result()

    monkeypatch.setattr(gen_cocotb.subprocess, "run", fake_run)

    makefiles_dir, diagnostics = gen_cocotb._cocotb_makefiles_dir()

    assert makefiles_dir == Path("/tmp/cocotb-makefiles")
    assert diagnostics == []
    assert calls == [[sys.executable, "-m", "cocotb_tools.config", "--makefiles"]]


def test_cocotb_common_missing_reports_install_command(monkeypatch) -> None:
    monkeypatch.setattr(gen_cocotb.importlib.util, "find_spec", lambda name: None if name == "cocotb" else object())
    monkeypatch.setattr(gen_cocotb.shutil, "which", lambda name: None if name == "make" else f"/usr/bin/{name}")

    diagnostics = gen_cocotb._cocotb_common_missing()

    assert 'python -m pip install -e ".[cocotb-smoke]"' in diagnostics[0]
    assert "make is not available on PATH" in diagnostics


def test_cocotb_makefile_path_uses_cygpath_for_msys_make(monkeypatch) -> None:
    class Result:
        returncode = 0
        stdout = "/c/project/rtl/dut.sv\n"
        stderr = ""

    monkeypatch.setattr(gen_cocotb, "_make_uses_msys", lambda: True)
    monkeypatch.setattr(gen_cocotb.shutil, "which", lambda name: "/usr/bin/cygpath" if name == "cygpath" else None)
    monkeypatch.setattr(gen_cocotb.subprocess, "run", lambda *args, **kwargs: Result())

    assert gen_cocotb._makefile_path(Path("C:/project/rtl/dut.sv")) == "/c/project/rtl/dut.sv"
