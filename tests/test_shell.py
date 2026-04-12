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
