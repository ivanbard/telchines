from __future__ import annotations

from pathlib import Path

import pytest

from telchines.config import ProjectConfig
from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from telchines.shell import (
    ShellCompleter,
    ShellSession,
    _build_fullscreen_shell_app,
    _dispatch_slash_command,
    _is_help_command,
    _parse_coverage_plan_args,
    _parse_gen_cocotb_args,
    _parse_gen_sva_args,
    _parse_repair_args,
    _parse_repeated_option,
    render_artifact_review_payload,
    render_help,
    render_replay_payload,
    render_runs_doctor_payload,
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
    assert "/runs [list|doctor|show RUN_ID|replay RUN_ID" in rendered
    assert "[--yes]]" in rendered
    assert "/artifacts [purge [--yes]|review REF]" in rendered
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


def test_shell_parser_accepts_repeated_workflow_options() -> None:
    tool, files, extra_args, apply_patch = _parse_repair_args(
        ["--tool", "iverilog", "--file", "rtl/a.sv", "--file", "rtl/b.sv", "--extra-arg", "-Wall", "--apply"]
    )
    assert tool == "iverilog"
    assert files == ["rtl/a.sv", "rtl/b.sv"]
    assert extra_args == ["-Wall"]
    assert apply_patch is True

    report, exclusions, formal_run, rtl_paths, spec_paths = _parse_coverage_plan_args(
        [
            "--report",
            "cov/coverage.json",
            "--exclusions",
            "cov/exclusions.json",
            "--formal-run",
            "run_formal",
            "--rtl",
            "rtl/a.sv",
            "--rtl",
            "rtl/b.sv",
            "--spec",
            "docs/a.md",
            "--spec",
            "docs/b.md",
        ]
    )
    assert report == "cov/coverage.json"
    assert exclusions == "cov/exclusions.json"
    assert formal_run == "run_formal"
    assert rtl_paths == ["rtl/a.sv", "rtl/b.sv"]
    assert spec_paths == ["docs/a.md", "docs/b.md"]

    assert _parse_repeated_option(["--logs", "logs/a.log", "--logs", "logs/b.log"], "--logs") == [
        "logs/a.log",
        "logs/b.log",
    ]


@pytest.mark.parametrize(
    ("parser", "parts", "message"),
    [
        (_parse_repair_args, ["--tool", "iverilog", "--file"], "--file requires a value"),
        (_parse_coverage_plan_args, ["--report", "cov/coverage.json", "--rtl"], "--rtl requires a value"),
        (_parse_gen_sva_args, ["--spec", "docs/spec.md", "--rtl"], "--rtl requires a value"),
        (_parse_gen_cocotb_args, ["--dut", "rtl/dut.sv", "--provider"], "--provider requires a value"),
        (lambda values: _parse_repeated_option(values, "--logs"), ["--logs"], "--logs requires a value"),
    ],
)
def test_shell_parsers_report_missing_values(parser, parts: list[str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parser(parts)


def test_shell_completes_commands_and_paths(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    completer = ShellCompleter(session)
    commands = list(completer.get_completions(Document("/pro"), None))
    assert any(item.text == "/providers" for item in commands)

    paths = list(completer.get_completions(Document("/triage --logs logs/reg"), None))
    assert any(item.text == "logs/regressions/" for item in paths)


def test_fullscreen_shell_accepts_pipe_input(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    session.add_transcript("Telchines", render_welcome(session))
    with create_pipe_input() as pipe_input:
        app = _build_fullscreen_shell_app(session, input=pipe_input, output=DummyOutput())
        pipe_input.send_text("/pwd\r/exit\r")
        app.run()

    assert session.history == ["/pwd", "/exit"]
    assert any(str(sample_project) in item for item in session.transcript)
    assert session.transcript[-1] == "leaving Telchines shell"


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


def test_shell_runs_doctor_rendering() -> None:
    rendered = render_runs_doctor_payload(
        {
            "status": "warning",
            "run_count": 1,
            "issue_count": 1,
            "issues": [
                {
                    "run_id": "run_corrupt",
                    "path": "runs/run_corrupt.json",
                    "error": "JSONDecodeError: bad json",
                }
            ],
        }
    )
    assert "Runs Doctor" in rendered
    assert "load issues: 1" in rendered
    assert "runs/run_corrupt.json" in rendered


def test_shell_artifact_review_rendering() -> None:
    rendered = render_artifact_review_payload(
        {
            "status": "modified",
            "generated_file": ".tel/artifacts/generated/cocotb/test_uart_rx.py",
            "candidate_id": "cocotb_1",
            "baseline_line_count": 10,
            "current_line_count": 11,
            "diff_line_count": 5,
            "diff_truncated": False,
            "diff": "--- stored\n+++ workspace\n+# human note",
        }
    )
    assert "Artifact Review" in rendered
    assert "modified" in rendered
    assert "human note" in rendered


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
