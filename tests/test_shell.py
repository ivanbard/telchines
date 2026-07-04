from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from telchines.config import ProjectConfig
from telchines.config import SUPPORTED_REASONING_LEVELS
from prompt_toolkit.document import Document
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from telchines.shell import (
    ShellCompleter,
    ShellSession,
    _ascii_safe_boxes,
    _build_fullscreen_shell_app,
    _dispatch_slash_command,
    _header_fragments,
    _is_help_command,
    _is_transcript_command,
    _parse_agent_args,
    _parse_coverage_plan_args,
    _parse_gen_cocotb_args,
    _parse_gen_sva_args,
    _parse_repair_args,
    _parse_repeated_option,
    _sidebar_text,
    render_artifact_review_payload,
    render_agent_payload,
    render_cocotb_payload,
    render_command_progress,
    render_help,
    render_provider_check_payload,
    render_repair_payload,
    render_replay_payload,
    render_runs_doctor_payload,
    render_run_show,
    render_sva_payload,
    render_welcome,
)
from telchines.utils import read_json, write_json


PATH_TEXT = st.from_regex(r"[A-Za-z0-9_/.-]{1,24}", fullmatch=True).filter(lambda value: not value.startswith("-"))
OPTION_VALUE = st.from_regex(r"[A-Za-z0-9_=./-]{1,24}", fullmatch=True).filter(lambda value: not value.startswith("--"))
IDENT = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,12}", fullmatch=True)
PROVIDER_NAME = st.from_regex(r"[A-Za-z][A-Za-z0-9_-]{0,12}", fullmatch=True).filter(lambda value: value != "heuristic")
MODEL_NAME = st.from_regex(r"[A-Za-z0-9_.:/-]{1,24}", fullmatch=True)
LIMITATION_TEXT = st.from_regex(r"[A-Za-z][A-Za-z0-9 _./:-]{0,40}", fullmatch=True).filter(lambda value: "None" not in value)
LIMITATION_LIST = st.lists(st.from_regex(r"lim_[0-9]{3}", fullmatch=True), max_size=6, unique=True)
OPTIONAL_STATUS = st.one_of(st.none(), st.from_regex(r"[A-Za-z][A-Za-z0-9_./:-]{0,20}", fullmatch=True))
BOX_TEXT = st.text(alphabet=list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_|+─━│┃┌┐└┘├┤┬┴┼╭╮╰╯"), max_size=80)
BOX_CHARS = set("─━│┃┌┐└┘├┤┬┴┼╭╮╰╯")


def _install_shell_model_policy(project_root: Path, provider_name: str = "local-test") -> None:
    config_path = project_root / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"] = {
        "default_provider_by_capability": {"repair": provider_name, "generation": "heuristic"},
        "providers": {
            "heuristic": {"kind": "heuristic", "capabilities": ["generation"]},
            provider_name: {
                "kind": "local_command",
                "capabilities": ["repair"],
                "command": "python",
                "args": ["tools/local_provider.py"],
                "model": "w-latest",
                "reasoning_level": "medium",
                "timeout_seconds": 5,
            },
        },
    }
    write_json(config_path, payload)


class NarrowOutput(DummyOutput):
    def get_size(self) -> Size:
        return Size(rows=12, columns=50)


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
    assert "/model [list|select|set|reasoning]" in rendered
    assert "/agent TASK" in rendered
    assert "/triage --logs PATH" in rendered
    assert "/coverage-plan --report PATH" in rendered
    assert "/gen-sva --spec PATH --rtl PATH" in rendered
    assert "/gen-cocotb --dut PATH" in rendered
    assert "/waveforms" in rendered
    assert "/runs [list|doctor|show RUN_ID|replay RUN_ID" in rendered
    assert "import MANIFEST" in rendered
    assert "/artifacts [purge [--yes]|review REF]" in rendered
    assert "/raw <slash command>" in rendered


def test_shell_detects_help_command() -> None:
    assert _is_help_command("/help") is True
    assert _is_help_command("help") is True
    assert _is_help_command("/providers") is False


def test_shell_model_list_command_renders_offline_picker(sample_project: Path) -> None:
    _install_shell_model_policy(sample_project)
    session = ShellSession(cwd=sample_project)
    _, rendered = _dispatch_slash_command(session, "model list --offline")
    assert "Model Selection" in rendered
    assert "local-test" in rendered
    assert "w-latest" in rendered
    assert "medium" in rendered
    assert "configured" in rendered
    assert "model alias" in rendered
    assert "may move" in rendered


def test_shell_model_commands_update_config_and_status_summaries(sample_project: Path) -> None:
    _install_shell_model_policy(sample_project)
    session = ShellSession(cwd=sample_project)

    _dispatch_slash_command(session, "model set --provider local-test --model wrapper-v2")
    _dispatch_slash_command(session, "model reasoning --provider local-test --level high")
    _dispatch_slash_command(session, "model select --capability repair --provider local-test")

    payload = read_json(sample_project / ".tel" / "config.json")
    provider = payload["project"]["model_policy"]["providers"]["local-test"]
    header = "".join(fragment for _, fragment in _header_fragments(session))
    sidebar = _sidebar_text(session)
    assert provider["model"] == "wrapper-v2"
    assert provider["reasoning_level"] == "high"
    assert "repair: local-test:wrapper-v2/high" in header
    assert "repair: local-test:wrapper-v2/high" in sidebar


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(provider_name=PROVIDER_NAME, model=MODEL_NAME, level=st.sampled_from(sorted(SUPPORTED_REASONING_LEVELS)))
def test_shell_model_commands_round_trip_generated_values(sample_project: Path, provider_name: str, model: str, level: str) -> None:
    _install_shell_model_policy(sample_project, provider_name)
    session = ShellSession(cwd=sample_project)

    _dispatch_slash_command(session, f"model set --provider {provider_name} --model {model}")
    _dispatch_slash_command(session, f"model reasoning --provider {provider_name} --level {level}")
    _dispatch_slash_command(session, f"model select --capability repair --provider {provider_name}")

    payload = read_json(sample_project / ".tel" / "config.json")
    provider = payload["project"]["model_policy"]["providers"][provider_name]
    assert payload["project"]["model_policy"]["default_provider_by_capability"]["repair"] == provider_name
    assert provider["model"] == model
    assert provider["reasoning_level"] == level


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("model select --capability repair --provider", "--provider requires a value"),
        ("model select --capability --provider local-test", "--capability requires a value"),
        ("model set --provider local-test --model", "--model requires a value"),
        ("model reasoning --provider local-test --level", "--level requires a value"),
    ],
)
def test_shell_model_commands_report_missing_values(sample_project: Path, command: str, message: str) -> None:
    _install_shell_model_policy(sample_project)
    session = ShellSession(cwd=sample_project)
    with pytest.raises(ValueError, match=message):
        _dispatch_slash_command(session, command)


def test_shell_parser_reports_missing_option_values(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    try:
        _dispatch_slash_command(session, "repair --tool fixture --file")
    except ValueError as exc:
        assert "--file requires a value" in str(exc)
    else:
        raise AssertionError("expected missing option value to raise")


def test_shell_parser_accepts_repeated_workflow_options() -> None:
    tool, files, extra_args, apply_patch, context = _parse_repair_args(
        [
            "--tool",
            "iverilog",
            "--file",
            "rtl/a.sv",
            "--file",
            "rtl/b.sv",
            "--extra-arg",
            "-Wall",
            "--filelist",
            "rtl/files.f",
            "--include-dir",
            "rtl/include",
            "--define",
            "SIM=1",
            "--top",
            "tb",
            "--apply",
        ]
    )
    assert tool == "iverilog"
    assert files == ["rtl/a.sv", "rtl/b.sv"]
    assert extra_args == ["-Wall"]
    assert apply_patch is True
    assert context["filelists"] == ["rtl/files.f"]
    assert context["include_dirs"] == ["rtl/include"]
    assert context["defines"] == ["SIM=1"]
    assert context["top_module"] == "tb"

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

    agent_args = _parse_agent_args(
        [
            "fix",
            "broken",
            "counter",
            "--tool",
            "fixture",
            "--file",
            "rtl/a.sv",
            "--file",
            "rtl/b.sv",
            "--extra-arg",
            "-Wall",
            "--adapter-arg",
            "-sv",
            "--worklib",
            "work",
        ]
    )
    assert agent_args["task"] == "fix broken counter"
    assert agent_args["tool"] == "fixture"
    assert agent_args["files"] == ["rtl/a.sv", "rtl/b.sv"]
    assert agent_args["extra_args"] == ["-Wall"]
    assert agent_args["adapter_args"] == ["-sv"]
    assert agent_args["work_library"] == "work"


@given(
    files=st.lists(PATH_TEXT, max_size=4),
    filelists=st.lists(PATH_TEXT, max_size=4),
    include_dirs=st.lists(PATH_TEXT, max_size=4),
    defines=st.lists(OPTION_VALUE, max_size=4),
    adapter_args=st.lists(OPTION_VALUE, max_size=4),
    top=IDENT,
    worklib=IDENT,
)
def test_shell_repair_parser_preserves_compile_context_options(
    files: list[str],
    filelists: list[str],
    include_dirs: list[str],
    defines: list[str],
    adapter_args: list[str],
    top: str,
    worklib: str,
) -> None:
    parts = ["--tool", "iverilog", "--top", top, "--worklib", worklib]
    for value in files:
        parts.extend(["--file", value])
    for value in filelists or ["fallback.f"]:
        parts.extend(["--filelist", value])
    for value in include_dirs:
        parts.extend(["--include-dir", value])
    for value in defines:
        parts.extend(["--define", value])
    for value in adapter_args:
        parts.extend(["--adapter-arg", value])

    tool, parsed_files, _, _, context = _parse_repair_args(parts)

    assert tool == "iverilog"
    assert parsed_files == files
    assert context["filelists"] == (filelists or ["fallback.f"])
    assert context["include_dirs"] == include_dirs
    assert context["defines"] == defines
    assert context["adapter_args"] == adapter_args
    assert context["top_module"] == top
    assert context["work_library"] == worklib


@given(
    adapter_args=st.lists(OPTION_VALUE, max_size=3),
    filelists=st.lists(PATH_TEXT, max_size=3),
    include_dirs=st.lists(PATH_TEXT, max_size=3),
    defines=st.lists(OPTION_VALUE, max_size=3),
    top=IDENT,
)
def test_shell_generation_parsers_share_compile_context(
    adapter_args: list[str],
    filelists: list[str],
    include_dirs: list[str],
    defines: list[str],
    top: str,
) -> None:
    context_parts: list[str] = ["--top", top]
    for option, values in (
        ("--adapter-arg", adapter_args),
        ("--filelist", filelists),
        ("--include-dir", include_dirs),
        ("--define", defines),
    ):
        for value in values:
            context_parts.extend([option, value])

    _, _, _, _, sva_context = _parse_gen_sva_args(["--spec", "docs/spec.md", "--rtl", "rtl/dut.sv", *context_parts])
    _, _, _, _, _, cocotb_context = _parse_gen_cocotb_args(["--dut", "rtl/dut.sv", *context_parts])

    for context in (sva_context, cocotb_context):
        assert context["adapter_args"] == adapter_args
        assert context["filelists"] == filelists
        assert context["include_dirs"] == include_dirs
        assert context["defines"] == defines
        assert context["top_module"] == top


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


def test_shell_completes_options_and_file_mentions(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    completer = ShellCompleter(session)

    triage_options = list(completer.get_completions(Document("/triage --"), None))
    assert {item.text for item in triage_options} >= {"--logs", "--waveform"}

    sva_options = list(completer.get_completions(Document("/gen-sva --"), None))
    assert {item.text for item in sva_options} >= {"--spec", "--rtl", "--output"}

    import_options = list(completer.get_completions(Document("/runs import --"), None))
    assert any(item.text == "--dry-run" for item in import_options)

    mentions = list(completer.get_completions(Document("/agent inspect @docs/u"), None))
    assert any(item.text == "@docs/uart.md" for item in mentions)


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


def test_fullscreen_shell_accepts_model_list_pipe_input(sample_project: Path) -> None:
    _install_shell_model_policy(sample_project)
    session = ShellSession(cwd=sample_project)
    session.add_transcript("Telchines", render_welcome(session))
    with create_pipe_input() as pipe_input:
        app = _build_fullscreen_shell_app(session, input=pipe_input, output=DummyOutput())
        pipe_input.send_text("/model list --offline\r/exit\r")
        app.run()

    assert session.history == ["/model list --offline", "/exit"]
    assert any("Model Selection" in item for item in session.transcript)


def test_fullscreen_shell_supports_history_navigation(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    session.add_transcript("Telchines", render_welcome(session))
    with create_pipe_input() as pipe_input:
        app = _build_fullscreen_shell_app(session, input=pipe_input, output=DummyOutput())
        pipe_input.send_text("/pwd\r\x1b[A\r/exit\r")
        app.run()

    assert session.history == ["/pwd", "/pwd", "/exit"]
    assert sum(1 for item in session.transcript if str(sample_project) in item) >= 2


@pytest.mark.parametrize("control", ["\x03", "\x04"])
def test_fullscreen_shell_control_keys_exit(sample_project: Path, control: str) -> None:
    session = ShellSession(cwd=sample_project)
    session.add_transcript("Telchines", render_welcome(session))
    with create_pipe_input() as pipe_input:
        app = _build_fullscreen_shell_app(session, input=pipe_input, output=DummyOutput())
        pipe_input.send_text(control)
        app.run()

    assert session.transcript[-1] == "leaving Telchines shell"


def test_fullscreen_shell_runs_in_narrow_terminal(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    session.add_transcript("Telchines", render_welcome(session))
    with create_pipe_input() as pipe_input:
        app = _build_fullscreen_shell_app(session, input=pipe_input, output=NarrowOutput())
        pipe_input.send_text("/pwd\r/exit\r")
        app.run()

    assert session.history == ["/pwd", "/exit"]
    assert session.transcript[-1] == "leaving Telchines shell"


def test_shell_history_and_transcript_commands(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    session.history.extend(["/providers", "/index"])
    session.transcript.append("hello")
    _, history = _dispatch_slash_command(session, "history")
    assert "1. /providers" in history
    _, transcript = _dispatch_slash_command(session, "transcript")
    assert "hello" in transcript
    assert _is_transcript_command("/transcript") is True
    assert _is_transcript_command("transcript") is True
    assert _is_transcript_command("/providers") is False
    _, cleared = _dispatch_slash_command(session, "clear")
    assert cleared == "transcript cleared"
    assert session.transcript == []


def test_shell_transcript_command_does_not_append_rendered_transcript(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    session.transcript.extend(["first", "second"])

    _, transcript = _dispatch_slash_command(session, "transcript")

    assert transcript == "first\n\nsecond"
    assert session.transcript == ["first", "second"]


@settings(max_examples=40)
@given(
    leading=st.text(alphabet=" \t", max_size=4),
    trailing=st.text(alphabet=" \t", max_size=4),
    command=st.sampled_from(["transcript", "TRANSCRIPT", "Transcript"]),
    slash=st.booleans(),
)
def test_transcript_command_detection_accepts_whitespace_case_and_optional_slash(
    leading: str,
    trailing: str,
    command: str,
    slash: bool,
) -> None:
    assert _is_transcript_command(f"{leading}{'/' if slash else ''}{command}{trailing}") is True


@settings(max_examples=40)
@given(text=st.text(alphabet=" \tABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-", max_size=80))
def test_ascii_safe_boxes_leaves_plain_ascii_unchanged(text: str) -> None:
    assert _ascii_safe_boxes(text) == text


@settings(max_examples=60)
@given(text=BOX_TEXT)
def test_ascii_safe_boxes_removes_box_characters_and_is_cp1252_safe(text: str) -> None:
    translated = _ascii_safe_boxes(text)

    assert not any(char in BOX_CHARS for char in translated)
    translated.encode("cp1252", errors="strict")


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


def test_shell_progress_and_provider_runtime_rendering() -> None:
    progress = render_command_progress('/agent "fix counter" --tool fixture --file rtl/broken_counter.sv')
    assert "Progress" in progress
    assert "waiting for model or workflow" in progress

    rendered = render_provider_check_payload(
        {
            "providers": [
                {
                    "name": "agent-repair",
                    "kind": "agent_runtime",
                    "status": "passed",
                    "summary": "provider check passed",
                    "checks": {
                        "transport": {
                            "mode": "agent_runtime",
                            "runtime": "langgraph",
                            "runtime_mode": "bounded_loop_no_langgraph",
                        }
                    },
                }
            ]
        }
    )
    assert "bounded_loop_no" in rendered
    assert "agent_runtime" in rendered


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


def test_shell_result_renderers_include_validation_mode() -> None:
    repair = render_repair_payload(
        {
            "run_id": "run_1",
            "provider": "heuristic",
            "status": "review_required",
            "patch_id": "patch_1",
            "proposal_explanation": "fix",
            "validation_status": "passed",
            "validation_mode": "adapter_replay",
            "validation_summary": "validation passed",
        }
    )
    agent = render_agent_payload(
        {
            "task_id": "task_1",
            "workflow_type": "compile_repair",
            "status": "review_required",
            "context_id": "ctx_1",
            "result": {
                "patch_id": "patch_1",
                "validation_run_id": "run_2",
                "validation_status": "passed",
                "validation_mode": "adapter_replay",
                "validation_limitations": ["adapter replay does not prove behavior"],
            },
            "evidence": {},
            "review_gate": {"summary": "review required"},
        }
    )
    sva = render_sva_payload(
        {
            "provider": "heuristic",
            "status": "validated",
            "artifact_path": ".tel/artifacts/generated/demo.sv",
            "validation_status": "passed",
            "validation_mode": "structure_only",
            "formal_status": "not_run",
            "validation_limitations": ["structural checks do not prove assertion semantics"],
            "explanation": "",
            "attempts": [],
            "rejected_candidate_ids": [],
            "property_summaries": [],
        }
    )
    cocotb = render_cocotb_payload(
        {
            "provider": "heuristic",
            "status": "validated",
            "top_module": "uart_rx",
            "artifact_path": ".tel/artifacts/generated/test_uart_rx.py",
            "manifest_path": ".tel/artifacts/generated/manifest.json",
            "validation_status": "passed",
            "validation_mode": "syntax_plus_structure",
            "executable_status": "skipped",
            "validation_limitations": ["built-in validation does not run a simulator"],
            "explanation": "",
            "attempts": [],
            "rejected_candidate_ids": [],
            "assumptions": [],
        }
    )

    assert "validation mode: adapter_replay" in repair
    assert "validation mode: adapter_replay" in agent
    assert "adapter replay does not prove behavior" in agent
    assert "validation mode: structure_only" in sva
    assert "formal status: not_run" in sva
    assert "did not prove:" in sva
    assert "assertion semantics" in sva
    assert "validation mode: syntax_plus_structure" in cocotb
    assert "executable status: skipped" in cocotb
    assert "does not run a simulator" in cocotb


def test_shell_generation_renderers_skip_empty_artifact_rows() -> None:
    rendered = render_sva_payload(
        {
            "provider": "heuristic",
            "status": "no_generation",
            "artifact_path": None,
            "validation_status": None,
            "validation_mode": None,
            "formal_status": None,
            "validation_limitations": [],
            "explanation": None,
            "attempts": [],
            "rejected_candidate_ids": [],
            "property_summaries": [],
        }
    )

    assert "status: no_generation" in rendered
    assert "artifact: None" not in rendered
    assert "validation: None" not in rendered


def _assert_limitation_rendering(rendered: str, limitations: list[str]) -> None:
    assert "None" not in rendered
    if limitations:
        assert "did not prove:" in rendered
        for item in limitations[:3]:
            assert f"- {item}" in rendered
        for item in limitations[3:]:
            assert f"- {item}" not in rendered
    else:
        assert "did not prove:" not in rendered


@settings(max_examples=50)
@given(validation_status=OPTIONAL_STATUS, validation_mode=OPTIONAL_STATUS, formal_status=OPTIONAL_STATUS, limitations=LIMITATION_LIST)
def test_sva_renderer_property_handles_optional_validation_fields(
    validation_status: str | None,
    validation_mode: str | None,
    formal_status: str | None,
    limitations: list[str],
) -> None:
    rendered = render_sva_payload(
        {
            "provider": "heuristic",
            "status": "validated",
            "artifact_path": ".tel/artifacts/generated/demo.sv",
            "validation_status": validation_status,
            "validation_mode": validation_mode,
            "formal_status": formal_status,
            "validation_limitations": limitations,
            "explanation": "",
            "attempts": [],
            "rejected_candidate_ids": [],
            "property_summaries": [],
        }
    )

    _assert_limitation_rendering(rendered, limitations)
    assert ("formal status:" in rendered) is bool(formal_status)
    rendered.encode("cp1252", errors="strict")


@settings(max_examples=50)
@given(validation_status=OPTIONAL_STATUS, validation_mode=OPTIONAL_STATUS, executable_status=OPTIONAL_STATUS, limitations=LIMITATION_LIST)
def test_cocotb_renderer_property_handles_optional_validation_fields(
    validation_status: str | None,
    validation_mode: str | None,
    executable_status: str | None,
    limitations: list[str],
) -> None:
    rendered = render_cocotb_payload(
        {
            "provider": "heuristic",
            "status": "validated",
            "top_module": "uart_rx",
            "artifact_path": ".tel/artifacts/generated/cocotb/test_uart_rx.py",
            "manifest_path": ".tel/artifacts/generated/cocotb/manifest.json",
            "validation_status": validation_status,
            "validation_mode": validation_mode,
            "executable_status": executable_status,
            "validation_limitations": limitations,
            "explanation": "",
            "attempts": [],
            "rejected_candidate_ids": [],
            "assumptions": [],
        }
    )

    _assert_limitation_rendering(rendered, limitations)
    assert ("executable status:" in rendered) is bool(executable_status)
    rendered.encode("cp1252", errors="strict")


@settings(max_examples=50)
@given(validation_status=OPTIONAL_STATUS, validation_mode=OPTIONAL_STATUS, executable_status=OPTIONAL_STATUS, limitations=LIMITATION_LIST)
def test_agent_renderer_property_handles_nested_validation_limitations(
    validation_status: str | None,
    validation_mode: str | None,
    executable_status: str | None,
    limitations: list[str],
) -> None:
    rendered = render_agent_payload(
        {
            "task_id": "task_1",
            "workflow_type": "dut_to_cocotb",
            "status": "review_required",
            "context_id": "ctx_1",
            "result": {
                "candidate_id": "cocotb_1",
                "validation_status": validation_status,
                "validation_mode": validation_mode,
                "executable_status": executable_status,
                "validation_limitations": limitations,
            },
            "evidence": {},
            "review_gate": {"summary": "review required"},
        }
    )

    _assert_limitation_rendering(rendered, limitations)
    assert ("executable status:" in rendered) is bool(executable_status)
    rendered.encode("cp1252", errors="strict")


@settings(max_examples=50)
@given(validation_mode=OPTIONAL_STATUS, formal_status=OPTIONAL_STATUS, executable_status=OPTIONAL_STATUS, limitations=LIMITATION_LIST)
def test_run_show_renderer_property_handles_validation_limitations(
    validation_mode: str | None,
    formal_status: str | None,
    executable_status: str | None,
    limitations: list[str],
) -> None:
    rendered = render_run_show(
        {
            "run_id": "run_1",
            "workflow_type": "dut_to_cocotb",
            "status": "validated",
            "tool": {"name": "heuristic"},
            "summary": "Generated artifact",
            "tool_result": {
                "status": "validated",
                "validation_mode": validation_mode,
                "formal_status": formal_status,
                "executable_status": executable_status,
                "validation_limitations": limitations,
            },
            "artifacts": {},
        }
    )

    _assert_limitation_rendering(rendered, limitations)
    assert ("formal status:" in rendered) is bool(formal_status)
    assert ("executable status:" in rendered) is bool(executable_status)
    rendered.encode("cp1252", errors="strict")


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
                "validation_mode": "syntax_plus_structure",
                "executable_status": "skipped",
                "validation_limitations": ["simulator execution was not run"],
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
    assert "validation mode: syntax_plus_structure" in rendered
    assert "executable status: skipped" in rendered
    assert "simulator execution was not run" in rendered
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
