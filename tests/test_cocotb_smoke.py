from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from telchines.cli import app
from telchines.config import ProjectConfig
from telchines.models import CocotbCandidate
from telchines.run_store import RunStore
from telchines.workflows.gen_cocotb import _cocotb_execution_contract, _cocotb_makefiles_dir, validate_cocotb_candidate

try:
    runner = CliRunner(mix_stderr=False)
except TypeError:
    runner = CliRunner()


def _require_executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        pytest.skip(f"{name} is not available on PATH")
    return path


def test_generated_cocotb_scaffold_runs_with_icarus_when_tools_are_available(sample_project: Path, monkeypatch) -> None:
    pytest.importorskip("cocotb")
    _, diagnostics = _cocotb_makefiles_dir()
    if diagnostics:
        pytest.skip("; ".join(diagnostics))
    _require_executable("iverilog")
    _require_executable("vvp")
    _require_executable("make")
    contract, diagnostics = _cocotb_execution_contract("icarus")
    if contract != "supported":
        pytest.skip("; ".join(diagnostics))

    monkeypatch.chdir(sample_project)
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0
    generation_result = runner.invoke(app, ["gen-cocotb", "--dut", "rtl/uart_rx.sv", "--spec", "docs/uart.md"])
    assert generation_result.exit_code == 0
    generated_payload = generation_result.stdout
    assert '"executable_status": "passed"' in generated_payload
    assert '"validation_mode": "compile_and_run"' in generated_payload


def test_cocotb_required_executable_smoke_fails_when_tools_missing(sample_project: Path, monkeypatch) -> None:
    config = ProjectConfig.load(sample_project)
    config.generation["cocotb"]["executable_smoke"] = "required"
    config.save()
    store = RunStore(config)
    candidate = CocotbCandidate(
        candidate_id="cocotb_required",
        task_id="task_required",
        dut_path="rtl/uart_rx.sv",
        spec_path=None,
        top_module="uart_rx",
        file_path=".tel/artifacts/generated/cocotb/test_uart_rx.py",
        manifest_path=".tel/artifacts/generated/cocotb/uart_rx_cocotb_manifest.json",
        candidate_content="import cocotb\n\n@cocotb.test()\nasync def test_uart_rx(dut):\n    pass\n",
        explanation="test",
        status="proposed",
    )
    monkeypatch.setattr("telchines.workflows.gen_cocotb._select_cocotb_simulator", lambda preferred: (None, ["no simulator"]))
    monkeypatch.setattr("telchines.workflows.gen_cocotb._cocotb_common_missing", lambda: [])
    monkeypatch.setattr("telchines.workflows.gen_cocotb._cocotb_makefiles_dir", lambda: (Path(), []))

    validation_run = validate_cocotb_candidate(config, store, candidate)

    assert validation_run.status == "failed"
    assert validation_run.tool_result["executable_status"] == "failed"
    assert validation_run.tool_result["setup_diagnostics"] == ["no simulator"]
