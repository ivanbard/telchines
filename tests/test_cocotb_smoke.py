from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from telchines.cli import app

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
    _require_executable("cocotb-config")
    _require_executable("iverilog")
    _require_executable("vvp")
    _require_executable("make")

    monkeypatch.chdir(sample_project)
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0
    generation_result = runner.invoke(app, ["gen-cocotb", "--dut", "rtl/uart_rx.sv", "--spec", "docs/uart.md"])
    assert generation_result.exit_code == 0

    generated_dir = sample_project / ".tel" / "artifacts" / "generated" / "cocotb"
    makefile = sample_project / "Makefile.cocotb-smoke"
    makefile.write_text(
        "\n".join(
            [
                "SIM ?= icarus",
                "TOPLEVEL_LANG = verilog",
                f"VERILOG_SOURCES = {sample_project / 'rtl' / 'uart_rx.sv'}",
                "TOPLEVEL = uart_rx",
                "MODULE = test_uart_rx",
                "include $(shell cocotb-config --makefiles)/Makefile.sim",
                "",
            ]
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(generated_dir) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        ["make", "-f", str(makefile), "SIM=icarus"],
        cwd=sample_project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
