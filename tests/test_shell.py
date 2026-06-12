from __future__ import annotations

from pathlib import Path

from telchines.config import ProjectConfig
from prompt_toolkit.document import Document

from telchines.shell import (
    ShellCompleter,
    ShellSession,
    _dispatch_slash_command,
    _is_help_command,
    render_help,
    render_replay_payload,
    render_run_show,
    render_welcome,
)


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
    assert "/providers [check [NAME] [--offline]]" in rendered
    assert "/triage --logs PATH" in rendered
    assert "/coverage-plan --report PATH" in rendered
    assert "/gen-sva --spec PATH --rtl PATH" in rendered
    assert "/gen-cocotb --dut PATH" in rendered
    assert "/waveforms" in rendered
    assert "/runs [list|show RUN_ID|replay RUN_ID [--yes]]" in rendered
    assert "/raw <slash command>" in rendered


def test_shell_detects_help_command() -> None:
    assert _is_help_command("/help") is True
    assert _is_help_command("help") is True
    assert _is_help_command("/providers") is False


def test_shell_parser_reports_missing_option_values(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    try:
        _dispatch_slash_command(session, "repair --tool fixture --file")
    except ValueError as exc:
        assert "--file requires a value" in str(exc)
    else:
        raise AssertionError("expected missing option value to raise")


def test_shell_completes_commands_and_paths(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    completer = ShellCompleter(session)
    commands = list(completer.get_completions(Document("/pro"), None))
    assert any(item.text == "/providers" for item in commands)

    paths = list(completer.get_completions(Document("/triage --logs logs/reg"), None))
    assert any(item.text == "logs/regressions/" for item in paths)


def test_shell_history_and_transcript_commands(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    session.history.extend(["/providers", "/index"])
    session.transcript.append("hello")
    _, history = _dispatch_slash_command(session, "history")
    assert "1. /providers" in history
    _, transcript = _dispatch_slash_command(session, "transcript")
    assert "hello" in transcript
    _, cleared = _dispatch_slash_command(session, "clear")
    assert cleared == "transcript cleared"
    assert session.transcript == []


def test_shell_replay_confirmation_rendering() -> None:
    rendered = render_replay_payload(
        {
            "status": "confirmation_required",
            "run_id": "run_1",
            "replay_command": ["iverilog", "rtl/demo.sv"],
        }
    )
    assert "Replay Confirmation" in rendered
    assert "not executed" in rendered
    assert "--yes" in rendered


def test_shell_supports_index_status_and_clean(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    _, status_before = _dispatch_slash_command(session, "index status")
    assert "Index Status" in status_before
    _, indexed = _dispatch_slash_command(session, "index")
    assert "Index Complete" in indexed
    _, cleaned = _dispatch_slash_command(session, "index clean")
    assert "Index Cleaned" in cleaned


def test_shell_supports_privacy_doctor(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    _, rendered = _dispatch_slash_command(session, "doctor privacy")
    assert '"no_egress"' in rendered


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


def test_render_run_show_includes_coverage_plan_details() -> None:
    rendered = render_run_show(
        {
            "run_id": "coverage_1",
            "workflow_type": "coverage_plan",
            "status": "passed",
            "tool": {"name": "coverage_assistant"},
            "summary": "Planned coverage actions",
            "tool_result": {
                "status": "planned",
                "report_source": "cov/coverage.json",
                "recommendation_count": 2,
                "formal_run_id": "formal_cov_1",
                "classifications": ["missing_stimulus", "missing_checker"],
            },
            "artifacts": {"coverage_plan_artifact": ".tel/task-artifacts/task_cov_plan.json"},
        }
    )
    assert "report source: cov/coverage.json" in rendered
    assert "recommendations: 2" in rendered
    assert "formal run: formal_cov_1" in rendered
    assert "classifications: missing_stimulus, missing_checker" in rendered
