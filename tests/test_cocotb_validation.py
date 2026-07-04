from __future__ import annotations

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
    monkeypatch.setattr(gen_cocotb.subprocess, "run", fake_run)

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

    class Result:
        returncode = 0
        stdout = "smoke passed\n"
        stderr = ""

    monkeypatch.setattr(gen_cocotb, "_select_cocotb_simulator", lambda preferred: ("icarus", []))
    monkeypatch.setattr(gen_cocotb, "_cocotb_common_missing", lambda: [])
    monkeypatch.setattr(gen_cocotb.subprocess, "run", lambda command, **kwargs: Result())

    validation_run = gen_cocotb.validate_cocotb_candidate(
        config,
        store,
        _candidate(),
        run_spec=AdapterRunSpec(
            files=["rtl/uart_rx.sv"],
            include_dirs=["rtl/include"],
            defines=["SIM=1"],
            extra_args=["-Wall"],
        ),
    )

    makefile = Path(validation_run.tool_result["command_artifacts"]["cocotb_smoke_makefile"])
    log_path = Path(validation_run.tool_result["command_artifacts"]["cocotb_smoke_log"])
    makefile_text = makefile.read_text(encoding="utf-8")
    assert "TOPLEVEL = uart_rx" in makefile_text
    assert "MODULE = test_uart_rx" in makefile_text
    assert "VERILOG_SOURCES" in makefile_text
    assert "COMPILE_ARGS += -Irtl/include -DSIM=1 -Wall" in makefile_text
    assert log_path.read_text(encoding="utf-8") == "smoke passed\n"


@given(has_import=st.booleans(), has_decorator=st.booleans())
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
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
