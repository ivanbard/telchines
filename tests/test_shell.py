from __future__ import annotations

from pathlib import Path

from telchines.config import ProjectConfig
from telchines.shell import ShellSession, _is_help_command, render_help, render_run_show, render_welcome


def test_shell_welcome_renders_project_context(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    rendered = render_welcome(session)
    config = ProjectConfig.load(sample_project)
    assert "Console-first shell" in rendered
    assert config.project.name in rendered
    assert "Repair" in rendered
    assert "Generate" in rendered


def test_shell_help_renders_core_commands() -> None:
    rendered = render_help()
    assert "/providers" in rendered
    assert "/triage --logs PATH" in rendered
    assert "/gen-sva --spec PATH --rtl PATH" in rendered
    assert "/gen-cocotb --dut PATH" in rendered
    assert "/waveforms" in rendered
    assert "/raw <slash command>" in rendered


def test_shell_detects_help_command() -> None:
    assert _is_help_command("/help") is True
    assert _is_help_command("help") is True
    assert _is_help_command("/providers") is False


def test_render_run_show_includes_formal_and_validation_details() -> None:
    rendered = render_run_show(
        {
            "run_id": "formal_1",
            "workflow_type": "formal_validation",
            "status": "failed",
            "tool": {"name": "symbiyosys"},
            "summary": "Formal run failed",
            "tool_result": {
                "status": "failed",
                "validation_mode": "compile_and_run",
                "property_ids": ["uart_prop"],
                "counterexample_paths": ["formal/trace.vcd"],
                "report_paths": ["formal/summary.txt"],
            },
            "artifacts": {"log_path": ".tel/artifacts/formal.log"},
        }
    )
    assert "validation mode: compile_and_run" in rendered
    assert "properties: uart_prop" in rendered
    assert "counterexamples: formal/trace.vcd" in rendered


def test_render_run_show_includes_cocotb_generation_details() -> None:
    rendered = render_run_show(
        {
            "run_id": "cocotb_1",
            "workflow_type": "dut_to_cocotb",
            "status": "validated",
            "tool": {"name": "heuristic"},
            "summary": "Generated cocotb scaffold",
            "tool_result": {
                "status": "validated",
                "top_module": "uart_rx",
                "assumptions": [
                    "Inferred `clk` as the primary clock.",
                    "Inferred `rst_n` as an active-low reset.",
                ],
            },
            "artifacts": {
                "generated_file": ".tel/artifacts/generated/cocotb/test_uart_rx.py",
                "manifest_path": ".tel/artifacts/generated/cocotb/uart_rx_cocotb_manifest.json",
            },
        }
    )
    assert "top module: uart_rx" in rendered
    assert "assumptions: Inferred `clk` as the primary clock." in rendered
    assert "generated_file=.tel/artifacts/generated/cocotb/test_uart_rx.py" in rendered
