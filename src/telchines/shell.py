from __future__ import annotations

import os
import shlex
import sys
import time
from dataclasses import dataclass, field
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import typer
from prompt_toolkit import Application
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, FormattedTextControl, HSplit, Layout, VSplit, Window
from prompt_toolkit.styles import Style
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.widgets import Frame, TextArea
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table

from telchines.config import ProjectConfig
from telchines.errors import AdapterExecutionError, ConfigError, ProviderError, TelchinesError
from telchines.operations import (
    agent,
    coverage_plan,
    check_providers,
    clean_index,
    doctor_runs,
    dump_json,
    format_coverage_human,
    format_triage_human,
    gen_cocotb,
    gen_sva,
    import_runs,
    inspect_waveform,
    index_project,
    index_status,
    initialize_project,
    list_adapters,
    list_model_options,
    list_providers,
    list_runs,
    list_waveforms,
    load_eval_report,
    privacy_report,
    purge_artifacts,
    repair,
    replay_run,
    retrieve_query,
    review_artifact,
    run_eval,
    select_model_provider,
    set_provider_model,
    set_provider_reasoning,
    show_run,
    show_waveform,
    triage,
    waveform_signals,
)

SHELL_COMMAND_HELP = [
    ("/help", "Show command reference"),
    ("/project init [path] [--name NAME]", "Initialize a Telchines project"),
    ("/index [status|clean]", "Build, inspect, or clean retrieval indexes"),
    ("/retrieve QUERY", "Search project context"),
    ("/providers [check [NAME] [--offline]]", "Show or check configured providers"),
    ("/model [list|select|set|reasoning]", "Choose provider, model, and reasoning defaults"),
    ("/agent TASK [--tool TOOL --file PATH]", "Plan and run a review-gated hardware agent task"),
    ("/repair --tool TOOL --file PATH", "Run repair workflow"),
    ("/triage --logs PATH [--logs PATH] [--waveform PATH]", "Run regression triage"),
    ("/coverage-plan --report PATH [--exclusions PATH] [--formal-run RUN_ID]", "Generate coverage closure recommendations"),
    ("/gen-sva --spec PATH --rtl PATH [--output PATH]", "Generate assertion draft from spec and RTL"),
    ("/gen-cocotb --dut PATH [--spec PATH] [--output-dir PATH]", "Generate a cocotb scaffold from DUT context"),
    ("/waveforms [list|show TARGET|signals TARGET|inspect TARGET --signal NAME]", "Inspect waveform summaries and signals"),
    ("/runs [list|doctor|show RUN_ID|replay RUN_ID [--yes]|import MANIFEST]", "Inspect or import stored runs"),
    ("/eval [run|report]", "Run or show benchmarks"),
    ("/doctor", "Show project/provider/adapter diagnostics"),
    ("/doctor privacy", "Show privacy and artifact-storage diagnostics"),
    ("/artifacts [purge [--yes]|review REF]", "Report, purge, or review generated artifacts"),
    ("/history", "Show shell command history"),
    ("/transcript", "Show the current shell transcript"),
    ("/clear", "Clear the shell transcript"),
    ("/cd PATH", "Change working directory"),
    ("/pwd", "Show current working directory"),
    ("/raw <slash command>", "Display raw JSON output"),
    ("/exit", "Leave the shell"),
]
SLASH_COMMANDS = [item[0].split()[0] for item in SHELL_COMMAND_HELP]
PATH_OPTIONS = {
    "--logs",
    "--waveform",
    "--file",
    "--rtl",
    "--spec",
    "--dut",
    "--report",
    "--exclusions",
    "--output",
    "--output-dir",
    "--filelist",
    "--include-dir",
}
SHELL_COMMAND_OPTIONS = {
    "/project init": ("--name",),
    "/providers check": ("--offline",),
    "/model list": ("--offline",),
    "/model select": ("--capability", "--provider"),
    "/model set": ("--provider", "--model"),
    "/model reasoning": ("--provider", "--level"),
    "/agent": (
        "--tool",
        "--file",
        "--extra-arg",
        "--adapter-arg",
        "--filelist",
        "--include-dir",
        "--define",
        "--top",
        "--worklib",
        "--apply",
        "--logs",
        "--waveform",
        "--report",
        "--exclusions",
        "--formal-run",
        "--rtl",
        "--spec",
        "--dut",
        "--output",
        "--output-dir",
        "--provider",
        "--intent",
    ),
    "/repair": ("--tool", "--file", "--extra-arg", "--adapter-arg", "--filelist", "--include-dir", "--define", "--top", "--worklib", "--apply"),
    "/triage": ("--logs", "--waveform"),
    "/coverage-plan": ("--report", "--exclusions", "--formal-run", "--rtl", "--spec"),
    "/gen-sva": ("--spec", "--rtl", "--output", "--provider", "--adapter-arg", "--filelist", "--include-dir", "--define", "--top", "--worklib"),
    "/gen-cocotb": ("--dut", "--spec", "--output-dir", "--intent", "--provider", "--adapter-arg", "--filelist", "--include-dir", "--define", "--top", "--worklib"),
    "/runs replay": ("--yes",),
    "/runs import": ("--dry-run",),
    "/waveforms signals": ("--filter",),
    "/waveforms inspect": ("--signal", "--window"),
    "/artifacts purge": ("--yes",),
}
REPEATABLE_OPTIONS = {"--file", "--extra-arg", "--adapter-arg", "--filelist", "--include-dir", "--define", "--logs", "--waveform", "--rtl", "--spec"}


@dataclass(slots=True)
class ShellSession:
    cwd: Path
    last_context_id: str | None = None
    recent_run_ids: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    transcript: list[str] = field(default_factory=list)

    def project_config(self) -> ProjectConfig | None:
        try:
            return ProjectConfig.discover(self.cwd)
        except ConfigError:
            return None

    def prompt(self) -> str:
        config = self.project_config()
        if config:
            return f"tel[{config.project.name}] {self.cwd.name}> "
        return f"tel {self.cwd.name}> "

    def active_provider(self) -> str:
        config = self.project_config()
        if not config:
            return "uninitialized"
        return config.default_provider_by_capability().get("repair", "heuristic")

    def active_generation_provider(self) -> str:
        config = self.project_config()
        if not config:
            return "uninitialized"
        return config.default_provider_by_capability().get("generation", "heuristic")

    def note_context(self, payload: dict[str, object]) -> None:
        context_id = payload.get("context_id")
        if isinstance(context_id, str) and context_id:
            self.last_context_id = context_id
        for key in ("run_id", "validation_run_id"):
            run_id = payload.get(key)
            if isinstance(run_id, str) and run_id and run_id not in self.recent_run_ids:
                self.recent_run_ids.insert(0, run_id)
        self.recent_run_ids = self.recent_run_ids[:5]

    def add_transcript(self, label: str, body: str) -> None:
        entry = f"{label}\n{body}".strip()
        self.transcript.append(entry)

    def indexed(self) -> bool:
        config = self.project_config()
        if not config:
            return False
        return (config.project_root / config.index_dir / "index.json").exists()

    def index_hint(self) -> str:
        config = self.project_config()
        if not config:
            return "no-project"
        try:
            payload = index_status(self.cwd)
        except (ConfigError, OSError):
            return "unknown"
        project = payload.get("project", {})
        if not project.get("exists"):
            return "missing"
        return str(payload.get("status", "unknown"))

    def logs_hint(self) -> str:
        candidate = _default_logs_path(self.cwd)
        return str(candidate.relative_to(self.cwd)) if candidate and candidate.exists() else "none"


@dataclass(slots=True)
class ShellViewState:
    help_visible: bool = False
    help_text: str = ""
    saved_input_text: str = ""
    saved_cursor_position: int = 0
    history_index: int | None = None


class ShellCompleter(Completer):
    def __init__(self, session: ShellSession) -> None:
        self.session = session

    def get_completions(self, document: Document, complete_event) -> Iterable[Completion]:  # noqa: ANN001
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        mention_seed = _file_mention_seed(text)
        if mention_seed is not None:
            token = f"@{mention_seed}"
            for value in _file_mention_completions(self.session.cwd, mention_seed):
                yield Completion(value, start_position=-len(token))
            return
        if _completing_command_name(text):
            for command in SLASH_COMMANDS:
                if command.startswith(text):
                    yield Completion(command, start_position=-len(text))
            return
        option_seed = _option_completion_seed(text)
        if option_seed is not None:
            for option in _option_completions(text, option_seed):
                yield Completion(option, start_position=-len(option_seed))
            return
        seed = _path_completion_seed(text)
        if seed is None:
            return
        for value in _path_completions(self.session.cwd, seed):
            yield Completion(value, start_position=-len(seed))


def run_shell(initial_cwd: Path | None = None, mode: str = "auto") -> None:
    session = ShellSession(cwd=(initial_cwd or Path.cwd()).resolve())
    session.add_transcript("Telchines", render_welcome(session))
    if _supports_fullscreen_shell(mode):
        _run_fullscreen_shell(session)
        return
    if mode == "fullscreen":
        typer.echo("Full-screen shell requested, but stdin/stdout are not TTYs; falling back to plain shell.")
    _run_basic_shell(session)


def _supports_fullscreen_shell(mode: str = "auto") -> bool:
    if mode == "plain" or os.environ.get("TELCHINES_PLAIN_SHELL") == "1":
        return False
    stdin = getattr(sys.stdin, "isatty", lambda: False)()
    stdout = getattr(sys.stdout, "isatty", lambda: False)()
    return bool(stdin and stdout)


def _run_basic_shell(session: ShellSession) -> None:
    typer.echo("Telchines interactive shell.")
    typer.echo("mode: plain")
    typer.echo(render_welcome(session))
    typer.echo("Type /help for commands, /exit to leave.")
    while True:
        typer.echo(session.prompt(), nl=False)
        line = sys.stdin.readline()
        if line == "":
            typer.echo("")
            return
        user_input = line.strip()
        if not user_input:
            continue
        session.history.append(user_input)
        session.transcript.append(f"{session.prompt()}{user_input}")
        progress = render_command_progress(user_input)
        if progress:
            session.transcript.append(progress)
            typer.echo(progress)
        try:
            should_exit, rendered = dispatch_input(session, user_input)
        except (ConfigError, ProviderError, AdapterExecutionError, ValueError, KeyError) as exc:
            typer.echo(f"error: {exc}")
            continue
        except TelchinesError as exc:
            typer.echo(f"error: {exc}")
            continue
        if rendered:
            session.transcript.append(rendered)
            typer.echo(rendered)
        if should_exit:
            return


def _run_fullscreen_shell(session: ShellSession) -> None:
    app = _build_fullscreen_shell_app(session)
    app.run()


def _build_fullscreen_shell_app(session: ShellSession, **app_kwargs: Any) -> Application:
    view_state = ShellViewState()
    transcript_area = TextArea(
        text="\n\n".join(session.transcript),
        read_only=True,
        scrollbar=True,
        focusable=False,
        wrap_lines=True,
    )
    input_history = InMemoryHistory()
    input_area = TextArea(
        height=1,
        prompt=session.prompt(),
        multiline=False,
        wrap_lines=False,
        completer=ShellCompleter(session),
        complete_while_typing=True,
        history=input_history,
    )
    help_area = TextArea(
        text="",
        read_only=True,
        scrollbar=True,
        focusable=True,
        wrap_lines=True,
    )

    header_window = Window(height=1, content=FormattedTextControl(text=lambda: _header_fragments(session)))
    sidebar_window = Window(content=FormattedTextControl(text=lambda: _sidebar_text(session)), wrap_lines=True)
    footer_window = Window(height=1, content=FormattedTextControl(text=lambda: _hint_fragments(session)))

    console_body = HSplit(
        [
            transcript_area,
            Window(height=1, char="-", style="class:subtle"),
            input_area,
        ]
    )

    console_frame = Frame(console_body, title="Console")
    help_frame = Frame(help_area, title="Help")
    left_pane = HSplit(
        [
            ConditionalContainer(content=console_frame, filter=Condition(lambda: not view_state.help_visible)),
            ConditionalContainer(content=help_frame, filter=Condition(lambda: view_state.help_visible)),
        ]
    )
    main_content = VSplit(
        [
            left_pane,
            Frame(sidebar_window, title="Status", width=Dimension(preferred=24, min=18, max=32)),
        ]
    )
    layout = Layout(
        HSplit(
            [
                header_window,
                main_content,
                footer_window,
            ]
        )
    )

    style = Style.from_dict(
        {
            "header": "bold fg:#d7e3ff bg:#1f2937",
            "subtle": "fg:#8fa1bf",
            "accent": "fg:#5fd7ff bold",
            "ok": "fg:#9ece6a",
            "warn": "fg:#e0af68",
            "error": "fg:#f7768e bold",
        }
    )

    kb = KeyBindings()

    def append_rendered(rendered: str) -> None:
        if rendered:
            session.transcript.append(rendered)
            transcript_area.text = "\n\n".join(session.transcript)
            transcript_area.buffer.cursor_position = len(transcript_area.text)

    def force_reflow() -> None:
        app.renderer.reset()
        app.renderer.clear()
        app.layout.reset()
        app.invalidate()

    def show_help_overlay() -> None:
        view_state.saved_input_text = input_area.text
        view_state.saved_cursor_position = input_area.buffer.cursor_position
        view_state.help_visible = True
        view_state.help_text = render_help()
        help_area.text = view_state.help_text
        help_area.buffer.cursor_position = 0
        force_reflow()
        app.layout.focus(help_area)

    def hide_help_overlay() -> None:
        if not view_state.help_visible:
            return
        view_state.help_visible = False
        input_area.text = view_state.saved_input_text
        input_area.buffer.cursor_position = min(view_state.saved_cursor_position, len(input_area.text))
        force_reflow()
        app.layout.focus(input_area)

    def submit() -> None:
        if view_state.help_visible:
            hide_help_overlay()
            return
        user_input = input_area.text.strip()
        if not user_input:
            return
        session.history.append(user_input)
        input_history.append_string(user_input)
        view_state.history_index = None
        if _is_help_command(user_input):
            show_help_overlay()
            input_area.text = ""
            input_area.prompt = session.prompt()
            return
        session.transcript.append(f"{session.prompt()}{user_input}")
        progress = render_command_progress(user_input)
        append_rendered(progress)
        try:
            should_exit, rendered = dispatch_input(session, user_input)
        except (ConfigError, ProviderError, AdapterExecutionError, ValueError, KeyError, TelchinesError) as exc:
            append_rendered(f"[error] {exc}")
            input_area.text = ""
            input_area.prompt = session.prompt()
            app.invalidate()
            return
        append_rendered(rendered)
        input_area.text = ""
        input_area.prompt = session.prompt()
        app.invalidate()
        if should_exit:
            app.exit()

    @kb.add("enter")
    def _(event) -> None:  # noqa: ANN001
        submit()

    @kb.add("up", filter=Condition(lambda: not view_state.help_visible))
    def _(event) -> None:  # noqa: ANN001
        if not session.history:
            return
        if view_state.history_index is None:
            view_state.history_index = len(session.history) - 1
        else:
            view_state.history_index = max(0, view_state.history_index - 1)
        input_area.text = session.history[view_state.history_index]
        input_area.buffer.cursor_position = len(input_area.text)

    @kb.add("down", filter=Condition(lambda: not view_state.help_visible))
    def _(event) -> None:  # noqa: ANN001
        if view_state.history_index is None:
            return
        if view_state.history_index >= len(session.history) - 1:
            view_state.history_index = None
            input_area.text = ""
        else:
            view_state.history_index += 1
            input_area.text = session.history[view_state.history_index]
        input_area.buffer.cursor_position = len(input_area.text)

    @kb.add("escape", filter=Condition(lambda: view_state.help_visible))
    @kb.add("q", filter=Condition(lambda: view_state.help_visible))
    def _(event) -> None:  # noqa: ANN001
        hide_help_overlay()

    @kb.add("c-c")
    @kb.add("c-d")
    def _(event) -> None:  # noqa: ANN001
        if view_state.help_visible:
            hide_help_overlay()
            return
        session.transcript.append("leaving Telchines shell")
        transcript_area.text = "\n\n".join(session.transcript)
        event.app.exit()

    app = Application(layout=layout, key_bindings=kb, full_screen=True, mouse_support=False, style=style, **app_kwargs)
    app.layout.focus(input_area)
    return app


def dispatch_input(session: ShellSession, user_input: str) -> tuple[bool, str]:
    if user_input.startswith("/"):
        return _dispatch_slash_command(session, user_input[1:])
    return _dispatch_plain_text(session, user_input)


def _dispatch_slash_command(session: ShellSession, command_line: str) -> tuple[bool, str]:
    try:
        parts = shlex.split(command_line)
    except ValueError as exc:
        raise ValueError(f"could not parse slash command: {exc}") from exc
    if not parts:
        return False, ""
    command = parts[0].lower()

    if command in {"exit", "quit"}:
        return True, "leaving Telchines shell"
    if command == "help":
        return False, render_help()
    if command == "pwd":
        return False, str(session.cwd)
    if command == "clear":
        session.transcript.clear()
        return False, "transcript cleared"
    if command == "history":
        return False, "\n".join(f"{index}. {item}" for index, item in enumerate(session.history, start=1)) or "history is empty"
    if command == "transcript":
        return False, "\n\n".join(session.transcript) or "transcript is empty"
    if command == "doctor":
        if len(parts) > 1 and parts[1] == "privacy":
            payload = privacy_report(session.cwd)
            return False, dump_json(payload)
        return False, render_doctor_payload(session)
    if command == "cd":
        target = Path(parts[1]) if len(parts) > 1 else Path.home()
        resolved = (session.cwd / target).resolve() if not target.is_absolute() else target.resolve()
        if not resolved.exists() or not resolved.is_dir():
            raise ValueError(f"directory does not exist: {resolved}")
        session.cwd = resolved
        return False, f"cwd -> {session.cwd}"
    if command == "raw":
        if len(parts) < 2:
            raise ValueError("/raw requires a slash command payload")
        payload = _execute_command(session, parts[1:], raw=True)
        return False, payload or ""

    started = time.perf_counter()
    payload = _execute_command(session, parts, raw=False)
    elapsed = time.perf_counter() - started
    if payload and command in {"providers", "agent", "repair", "gen-sva", "gen-cocotb", "runs", "artifacts"}:
        payload = f"{payload}\n\nelapsed: {elapsed:.2f}s"
    return False, payload or ""


def render_command_progress(user_input: str) -> str:
    if not user_input.startswith("/") or user_input.startswith("/raw "):
        return ""
    try:
        parts = shlex.split(user_input[1:])
    except ValueError:
        return ""
    if not parts:
        return ""
    phases = _progress_phases(parts)
    if not phases:
        return ""
    return render_action_panel("Progress", "\n".join(f"- {phase}" for phase in phases))


def _progress_phases(parts: list[str]) -> list[str]:
    command = parts[0].lower()
    if command == "providers" and len(parts) > 1 and parts[1] == "check":
        return ["provider selected", "request sent", "waiting for provider transport", "check result pending"]
    if command == "agent":
        return ["provider selected", "request sent", "waiting for model or workflow", "candidate/evidence collection pending", "validation/review state pending"]
    if command == "repair":
        return ["provider selected", "request sent", "waiting for repair candidate", "validation running", "review state pending"]
    if command in {"gen-sva", "gen-cocotb"}:
        return ["provider selected", "request sent", "waiting for generated artifact", "validation running", "artifact saved for review"]
    if command == "runs" and len(parts) > 1 and parts[1] == "show":
        return ["loading stored run", "collecting validation and artifact metadata"]
    if command == "artifacts" and len(parts) > 1 and parts[1] == "review":
        return ["loading stored candidate", "comparing workspace artifact", "collecting validation attempts"]
    return []


def _dispatch_plain_text(session: ShellSession, user_input: str) -> tuple[bool, str]:
    lowered = user_input.lower()
    if "provider" in lowered:
        return False, _render_intent("Inspect providers", render_provider_payload(list_providers(session.cwd)))
    if "index" in lowered:
        chunk_count = index_project(session.cwd)
        return False, _render_intent("Index project", f"indexed {chunk_count} chunks")
    if "triage" in lowered:
        logs_path = _default_logs_path(session.cwd)
        if logs_path is None:
            return False, "I can run triage, but I need a log path. Try `/triage --logs path/to/logs`."
        payload = triage(session.cwd, [logs_path])
        session.note_context(payload)
        return False, _render_intent("Run triage", format_triage_human(payload))
    if "coverage" in lowered:
        return False, "I can run coverage planning, but I need a coverage report path. Try `/coverage-plan --report cov/coverage.json`."
    if "waveform" in lowered or "trace" in lowered:
        payload = list_waveforms(session.cwd)
        return False, _render_intent("Inspect waveforms", render_waveform_list_payload(payload))
    if any(keyword in lowered for keyword in ("retrieve", "search", "find")):
        payload = retrieve_query(session.cwd, user_input, mode="general")
        session.note_context(payload)
        return False, _render_intent("Retrieve project context", render_retrieval_payload(payload))
    if "run" in lowered:
        return False, _render_intent("Inspect recent runs", render_runs_payload(list_runs(session.cwd)))
    if "repair" in lowered:
        return False, "I can run repair, but I need an explicit tool and file. Try `/repair --tool verilator --file rtl/foo.sv`."
    if "cocotb" in lowered or "testbench" in lowered:
        return False, "I can generate a cocotb scaffold, but I need a DUT path. Try `/gen-cocotb --dut rtl/uart_rx.sv --spec docs/uart.md`."
    if "assert" in lowered or "sva" in lowered:
        return False, "I can generate assertions, but I need a spec and RTL target. Try `/gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv`."
    return False, "I did not recognize that request. Use /help or try `/providers`, `/index`, or `/triage --logs logs/regressions`."


def _execute_command(session: ShellSession, parts: list[str], raw: bool) -> str | None:
    command = parts[0].lower()

    if command == "project" and len(parts) > 1 and parts[1] == "init":
        path, name = _parse_project_init(parts[2:])
        config = initialize_project((session.cwd / path).resolve() if not path.is_absolute() else path.resolve(), name=name)
        session.cwd = config.project_root
        payload = {"project_id": config.project.project_id, "root_path": config.project.root_path}
        return dump_json(payload) if raw else render_action_panel("Project Initialized", f"root: {config.project.root_path}\nproject: {config.project.project_id}")

    if command == "index":
        if len(parts) > 1 and parts[1] == "status":
            payload = index_status(session.cwd)
            return dump_json(payload) if raw else render_index_status_payload(payload)
        if len(parts) > 1 and parts[1] == "clean":
            payload = clean_index(session.cwd)
            return dump_json(payload) if raw else render_action_panel("Index Cleaned", f"removed {payload['removed_count']} index directorie(s)")
        if len(parts) > 1:
            raise ValueError("supported /index commands are status and clean")
        chunk_count = index_project(session.cwd)
        payload = {"indexed_chunks": chunk_count}
        return dump_json(payload) if raw else render_action_panel("Index Complete", f"indexed {chunk_count} chunks")

    if command == "artifacts":
        if len(parts) > 1 and parts[1] == "purge":
            payload = purge_artifacts(session.cwd, dry_run="--yes" not in parts[2:])
            return dump_json(payload) if raw else render_artifact_purge_payload(payload)
        if len(parts) > 2 and parts[1] == "review":
            payload = review_artifact(session.cwd, reference=parts[2])
            return dump_json(payload) if raw else render_artifact_review_payload(payload)
        raise ValueError("supported /artifacts commands are purge [--yes] and review <ref>")

    if command == "retrieve":
        query = " ".join(parts[1:]).strip()
        if not query:
            raise ValueError("/retrieve requires a query")
        payload = retrieve_query(session.cwd, query, mode="general")
        session.note_context(payload)
        return dump_json(payload) if raw else render_retrieval_payload(payload)

    if command == "providers":
        if len(parts) > 1 and parts[1] == "check":
            provider_name = None
            live = True
            for part in parts[2:]:
                if part == "--offline":
                    live = False
                    continue
                if provider_name is not None:
                    raise ValueError("/providers check accepts at most one provider name")
                provider_name = part
            payload = check_providers(session.cwd, provider_name=provider_name, live=live)
            return dump_json(payload) if raw else render_provider_check_payload(payload)
        payload = list_providers(session.cwd)
        return dump_json(payload) if raw else render_provider_payload(payload)

    if command == "model":
        action = parts[1].lower() if len(parts) > 1 else "list"
        if action == "list":
            payload = list_model_options(session.cwd, live="--offline" not in parts[2:])
            return dump_json(payload) if raw else render_model_options_payload(payload)
        if action == "select":
            capability = _parse_required_argument(parts[2:], "--capability")
            provider = _parse_required_argument(parts[2:], "--provider")
            payload = select_model_provider(session.cwd, capability, provider)
            return dump_json(payload) if raw else render_model_update_payload(payload)
        if action == "set":
            provider = _parse_required_argument(parts[2:], "--provider")
            model = _parse_required_argument(parts[2:], "--model")
            payload = set_provider_model(session.cwd, provider, model)
            return dump_json(payload) if raw else render_model_update_payload(payload)
        if action == "reasoning":
            provider = _parse_required_argument(parts[2:], "--provider")
            level = _parse_required_argument(parts[2:], "--level")
            payload = set_provider_reasoning(session.cwd, provider, level)
            return dump_json(payload) if raw else render_model_update_payload(payload)
        raise ValueError("supported /model commands are list, select, set, and reasoning")

    if command == "agent":
        agent_args = _parse_agent_args(parts[1:])
        payload = agent(
            session.cwd,
            agent_args["task"],
            tool=agent_args["tool"],
            files=agent_args["files"],
            extra_arg=agent_args["extra_args"],
            apply_patch=agent_args["apply_patch"],
            logs=[_resolve_path(session.cwd, value) for value in agent_args["logs"]],
            waveforms=[_resolve_path(session.cwd, value) for value in agent_args["waveforms"]],
            report=None if agent_args["report"] is None else _resolve_path(session.cwd, agent_args["report"]),
            exclusions=None if agent_args["exclusions"] is None else _resolve_path(session.cwd, agent_args["exclusions"]),
            formal_run_id=agent_args["formal_run"],
            rtl=[_resolve_path(session.cwd, value) for value in agent_args["rtl"]],
            spec=[_resolve_path(session.cwd, value) for value in agent_args["spec"]],
            dut=None if agent_args["dut"] is None else _resolve_path(session.cwd, agent_args["dut"]),
            output=None if agent_args["output"] is None else _resolve_path(session.cwd, agent_args["output"]),
            output_dir=None if agent_args["output_dir"] is None else _resolve_path(session.cwd, agent_args["output_dir"]),
            provider_name=agent_args["provider"],
            intent=agent_args["intent"],
            adapter_args=agent_args["adapter_args"],
            filelists=agent_args["filelists"],
            include_dirs=agent_args["include_dirs"],
            defines=agent_args["defines"],
            top_module=agent_args["top_module"],
            work_library=agent_args["work_library"],
        )
        session.note_context(payload)
        return dump_json(payload) if raw else render_agent_payload(payload)

    if command == "repair":
        tool, files, extra_args, apply_patch, context = _parse_repair_args(parts[1:])
        payload = repair(
            session.cwd,
            tool=tool,
            files=files,
            extra_arg=extra_args,
            apply_patch=apply_patch,
            adapter_args=context["adapter_args"],
            filelists=context["filelists"],
            include_dirs=context["include_dirs"],
            defines=context["defines"],
            top_module=context["top_module"],
            work_library=context["work_library"],
        )
        session.note_context(payload)
        return dump_json(payload) if raw else render_repair_payload(payload)

    if command == "triage":
        logs = _parse_repeated_option(parts[1:], "--logs", strict=False)
        waveforms = _parse_repeated_option(parts[1:], "--waveform", strict=False)
        if not logs:
            raise ValueError("/triage requires at least one --logs path")
        payload = triage(
            session.cwd,
            [_resolve_path(session.cwd, value) for value in logs],
            waveforms=[_resolve_path(session.cwd, value) for value in waveforms] if waveforms else None,
        )
        session.note_context(payload)
        return dump_json(payload) if raw else render_triage_payload(payload)

    if command == "coverage-plan":
        report, exclusions, formal_run, rtl_paths, spec_paths = _parse_coverage_plan_args(parts[1:])
        payload = coverage_plan(
            session.cwd,
            report=_resolve_path(session.cwd, report),
            exclusions=None if exclusions is None else _resolve_path(session.cwd, exclusions),
            formal_run_id=formal_run,
            rtl=[_resolve_path(session.cwd, value) for value in rtl_paths],
            spec=[_resolve_path(session.cwd, value) for value in spec_paths],
        )
        session.note_context(payload)
        return dump_json(payload) if raw else render_coverage_payload(payload)

    if command == "gen-sva":
        spec, rtl, output, provider, context = _parse_gen_sva_args(parts[1:])
        payload = gen_sva(
            session.cwd,
            spec=_resolve_path(session.cwd, spec),
            rtl=_resolve_path(session.cwd, rtl),
            output=None if output is None else _resolve_path(session.cwd, output),
            provider_name=provider,
            adapter_args=context["adapter_args"],
            filelists=context["filelists"],
            include_dirs=context["include_dirs"],
            defines=context["defines"],
            top_module=context["top_module"],
            work_library=context["work_library"],
        )
        session.note_context(payload)
        return dump_json(payload) if raw else render_sva_payload(payload)

    if command == "gen-cocotb":
        dut, spec, output_dir, intent, provider, context = _parse_gen_cocotb_args(parts[1:])
        payload = gen_cocotb(
            session.cwd,
            dut=_resolve_path(session.cwd, dut),
            spec=None if spec is None else _resolve_path(session.cwd, spec),
            output_dir=None if output_dir is None else _resolve_path(session.cwd, output_dir),
            intent=intent,
            provider_name=provider,
            adapter_args=context["adapter_args"],
            filelists=context["filelists"],
            include_dirs=context["include_dirs"],
            defines=context["defines"],
            top_module=context["top_module"],
            work_library=context["work_library"],
        )
        session.note_context(payload)
        return dump_json(payload) if raw else render_cocotb_payload(payload)

    if command == "runs":
        if len(parts) == 1 or parts[1] == "list":
            payload = list_runs(session.cwd)
            return dump_json(payload) if raw else render_runs_payload(payload)
        if parts[1] == "doctor":
            payload = doctor_runs(session.cwd)
            return dump_json(payload) if raw else render_runs_doctor_payload(payload)
        if parts[1] == "show" and len(parts) > 2:
            payload = show_run(session.cwd, parts[2])
            return dump_json(payload) if raw else render_run_show(payload)
        if parts[1] == "replay" and len(parts) > 2:
            payload = replay_run(session.cwd, parts[2], confirm="--yes" in parts[3:])
            return dump_json(payload) if raw else render_replay_payload(payload)
        if parts[1] == "import" and len(parts) > 2:
            payload = import_runs(session.cwd, _resolve_path(session.cwd, parts[2]), dry_run="--dry-run" in parts[3:])
            return dump_json(payload) if raw else render_import_runs_payload(payload)
        raise ValueError("supported /runs commands are list, doctor, show <run_id>, replay <run_id> [--yes], and import <manifest> [--dry-run]")

    if command == "eval":
        if len(parts) == 1 or parts[1] == "run":
            payload = run_eval(session.cwd)
            return dump_json(payload) if raw else render_eval_payload(payload)
        if parts[1] == "report":
            payload = load_eval_report(session.cwd)
            return dump_json(payload) if raw else render_eval_payload(payload)
        raise ValueError("supported /eval commands are run and report")

    if command == "waveforms":
        if len(parts) == 1 or parts[1] == "list":
            payload = list_waveforms(session.cwd)
            return dump_json(payload) if raw else render_waveform_list_payload(payload)
        if parts[1] == "show" and len(parts) > 2:
            payload = show_waveform(session.cwd, parts[2])
            return dump_json(payload) if raw else render_waveform_show_payload(payload)
        if parts[1] == "signals" and len(parts) > 2:
            signal_filter = _parse_optional_argument(parts[3:], "--filter")
            payload = waveform_signals(session.cwd, parts[2], signal_filter=signal_filter)
            return dump_json(payload) if raw else render_waveform_signals_payload(payload)
        if parts[1] == "inspect" and len(parts) > 2:
            signal = _parse_required_argument(parts[3:], "--signal")
            window_value = _parse_optional_argument(parts[3:], "--window")
            window = int(window_value) if window_value else 8
            payload = inspect_waveform(session.cwd, parts[2], signal=signal, window=window)
            return dump_json(payload) if raw else render_waveform_inspect_payload(payload)
        raise ValueError("supported /waveforms commands are list, show <target>, signals <target>, and inspect <target> --signal NAME")

    raise ValueError(f"unknown slash command: /{command}")


def render_welcome(session: ShellSession) -> str:
    config = session.project_config()
    table = Table.grid(padding=(0, 1))
    table.add_column(style="cyan", justify="right")
    table.add_column(style="white")
    table.add_row("Shell", "ready")
    table.add_row("Project", config.project.name if config else "No Telchines project detected")
    table.add_row("Repair", session.active_provider())
    table.add_row("Generate", session.active_generation_provider())
    table.add_row("Try", "/help  /providers  /index  /triage --logs logs/regressions")
    return _render_rich(
        Panel(
            table,
            title="Telchines",
            subtitle="Console-first shell",
            border_style="cyan",
        )
    )


def render_help() -> str:
    table = Table(title="Telchines Shell Commands", show_header=True, header_style="bold cyan")
    table.add_column("Command", style="white")
    table.add_column("Purpose", style="white")
    for command, purpose in SHELL_COMMAND_HELP:
        table.add_row(command, purpose)
    return _render_rich(table)


def _is_help_command(user_input: str) -> bool:
    stripped = user_input.strip().lower()
    return stripped in {"/help", "help"}


def render_provider_payload(payload: dict[str, object]) -> str:
    defaults = payload.get("default_provider_by_capability", {})
    table = Table(title="Provider Status", show_header=True, header_style="bold cyan")
    table.add_column("Provider")
    table.add_column("Kind")
    table.add_column("Capabilities")
    table.add_column("Default For")
    table.add_column("Model/Base")
    table.add_column("Reasoning")
    table.add_column("Runtime")
    table.add_column("Status")
    for provider in payload["providers"]:
        status = "allowed" if provider["allowed"] else f"blocked: {provider['blocked_reason']}"
        model_or_base = str(provider.get("model") or provider.get("base_provider") or "n/a")
        runtime = str(provider.get("runtime") or "n/a")
        timeout = provider.get("timeout_seconds")
        if timeout:
            runtime = f"{runtime} ({timeout}s)" if runtime != "n/a" else f"timeout {timeout}s"
        reasoning = str(provider.get("reasoning_level") or "auto")
        wire = str(provider.get("reasoning_wire_format") or "none")
        if wire != "none":
            reasoning = f"{reasoning}/{wire}"
        table.add_row(
            provider["name"],
            provider["kind"],
            ", ".join(provider["capabilities"]),
            ", ".join(provider["default_for"]) or "none",
            model_or_base,
            reasoning,
            runtime,
            status,
        )
    if defaults:
        defaults_lines = [f"{capability}: {provider_name}" for capability, provider_name in defaults.items()]
    else:
        defaults_lines = ["none configured"]

    group = Group(
        Panel("\n".join(defaults_lines), title="Default Providers", border_style="green"),
        table,
    )
    return _render_rich(group)


def render_model_options_payload(payload: dict[str, object]) -> str:
    defaults = payload.get("default_provider_by_capability", {})
    defaults_text = "\n".join(f"{capability}: {provider}" for capability, provider in dict(defaults).items()) or "none configured"
    table = Table(title="Model Selection", show_header=True, header_style="bold cyan")
    table.add_column("Provider")
    table.add_column("Capabilities")
    table.add_column("Default")
    table.add_column("Selected Model")
    table.add_column("Reasoning")
    table.add_column("Available Models")
    table.add_column("Discovery")
    for provider in payload.get("providers", []):
        if not isinstance(provider, dict):
            continue
        warnings = provider.get("model_warnings") or []
        warning_text = f" ({'; '.join(str(item) for item in warnings)})" if warnings else ""
        discovery = str(provider.get("discovery_status") or provider.get("model_source") or "configured")
        if provider.get("discovery_error"):
            discovery = f"{discovery}: {provider['discovery_error']}"
        reasoning = str(provider.get("reasoning_level") or "auto")
        wire = str(provider.get("reasoning_wire_format") or "none")
        if wire != "none":
            reasoning = f"{reasoning} / {wire}"
        models = provider.get("models") if isinstance(provider.get("models"), list) else []
        table.add_row(
            str(provider.get("name") or ""),
            ", ".join(str(item) for item in provider.get("capabilities", [])),
            ", ".join(str(item) for item in provider.get("default_for", [])) or "none",
            str(provider.get("model") or "wrapper-managed"),
            reasoning + warning_text,
            "\n".join(str(item) for item in models[:5]) or "n/a",
            discovery,
        )
    help_text = "\n".join(
        [
            "/model select --capability repair --provider NAME",
            "/model set --provider NAME --model MODEL",
            "/model reasoning --provider NAME --level auto|none|minimal|low|medium|high|xhigh",
        ]
    )
    return _render_rich(Group(Panel(defaults_text, title="Active Defaults", border_style="green"), table, Panel(help_text, title="Commands", border_style="cyan")))


def render_model_update_payload(payload: dict[str, object]) -> str:
    lines = [f"{key}: {value}" for key, value in payload.items() if key != "default_provider_by_capability"]
    defaults = payload.get("default_provider_by_capability")
    if isinstance(defaults, dict):
        lines.append("defaults: " + ", ".join(f"{capability}={provider}" for capability, provider in defaults.items()))
    return render_action_panel("Model Selection Updated", "\n".join(lines))


def render_provider_check_payload(payload: dict[str, object]) -> str:
    table = Table(title="Provider Checks", show_header=True, header_style="bold cyan")
    table.add_column("Provider")
    table.add_column("Kind")
    table.add_column("Status")
    table.add_column("Transport")
    table.add_column("Runtime")
    table.add_column("Summary")
    for provider in payload["providers"]:
        checks = provider.get("checks", {}) if isinstance(provider, dict) else {}
        transport = checks.get("transport", {}) if isinstance(checks, dict) else {}
        transport_mode = transport.get("mode") if isinstance(transport, dict) else None
        model = transport.get("model") if isinstance(transport, dict) else None
        reasoning = transport.get("reasoning_level") if isinstance(transport, dict) else None
        runtime_mode = transport.get("runtime_mode") if isinstance(transport, dict) else None
        runtime_text = str(runtime_mode or transport.get("runtime") or "n/a") if isinstance(transport, dict) else "n/a"
        transport_text = str(model or transport_mode or "n/a")
        if reasoning:
            transport_text = f"{transport_text} ({reasoning})"
        table.add_row(provider["name"], provider["kind"], provider["status"], transport_text, runtime_text, provider["summary"])
    return _render_rich(table)


def render_index_status_payload(payload: dict[str, object]) -> str:
    table = Table(title="Index Status", show_header=True, header_style="bold cyan")
    table.add_column("Index")
    table.add_column("State")
    table.add_column("Chunks")
    table.add_column("Sources")
    table.add_column("Missing/Stale/Deleted")
    for label in ("project", "external"):
        item = payload[label]
        table.add_row(
            label,
            "stale" if item["stale"] else "fresh",
            str(item["chunk_count"]),
            str(item["source_count"]),
            f"{item['missing_source_count']}/{item['stale_source_count']}/{item['deleted_source_count']}",
        )
    return _render_rich(table)


def render_artifact_purge_payload(payload: dict[str, object]) -> str:
    title = "Artifact Purge Plan" if payload["dry_run"] else "Artifacts Purged"
    body = [
        f"status: {payload['status']}",
        f"files: {payload['file_count']}",
        f"bytes: {payload['byte_count']}",
    ]
    for target in payload["targets"][:6]:
        body.append(f"- {target['path']} ({target['file_count']} files)")
    return render_action_panel(title, "\n".join(body))


def render_artifact_review_payload(payload: dict[str, object]) -> str:
    diff = str(payload.get("diff", ""))
    diff_preview = "\n".join(diff.splitlines()[:16])
    body = [
        f"status: {payload['status']}",
        f"file: {payload['generated_file']}",
        f"candidate: {payload['candidate_id']}",
        f"lines: stored={payload['baseline_line_count']} workspace={payload['current_line_count']}",
        f"diff lines: {payload['diff_line_count']}" + (" (truncated)" if payload.get("diff_truncated") else ""),
    ]
    attempts = payload.get("validation_attempts") or []
    if isinstance(attempts, list) and attempts:
        body.append("attempts:")
        for item in attempts[:4]:
            if isinstance(item, dict):
                body.append(f"- {item.get('attempt')}: {item.get('result')} run={item.get('run_id')}")
    generation_attempts = payload.get("attempts") or []
    if isinstance(generation_attempts, list) and generation_attempts:
        body.append("generation attempts:")
        for item in generation_attempts[:4]:
            if isinstance(item, dict):
                body.append(f"- {item.get('attempt')}: {item.get('status')} validation={item.get('validation_status')}")
    rejected = payload.get("rejected_candidate_ids") or []
    if isinstance(rejected, list) and rejected:
        body.append(f"rejected candidates: {', '.join(str(item) for item in rejected[:3])}")
    replay_artifacts = payload.get("replay_artifacts") or {}
    if isinstance(replay_artifacts, dict) and replay_artifacts:
        body.append("replay: " + ", ".join(f"{key}={value}" for key, value in list(replay_artifacts.items())[:3]))
    if diff_preview:
        body.extend(["", diff_preview])
    return render_action_panel("Artifact Review", "\n".join(body))


def render_doctor_payload(session: ShellSession) -> str:
    config = session.project_config()
    if config is None:
        return render_action_panel("Doctor", "No Telchines project detected. Run `/project init .` from a repository root.")
    providers = check_providers(session.cwd, live=False)
    adapters = list_adapters(session.cwd)
    provider_status = providers["status"]
    available_adapters = sum(1 for item in adapters["adapters"] if item["available"])
    adapter_total = len(adapters["adapters"])
    lines = [
        f"project: {config.project.name}",
        f"root: {config.project_root}",
        f"index: {'present' if session.indexed() else 'missing'}",
        f"providers: {provider_status}",
        f"adapters available: {available_adapters}/{adapter_total}",
        f"privacy: task artifacts are stored under {config.store_dir}/task-artifacts",
    ]
    return render_action_panel("Doctor", "\n".join(lines))


def render_retrieval_payload(payload: dict[str, object]) -> str:
    table = Table(title=f"Retrieval Hits {payload['context_id']}", show_header=True, header_style="bold cyan")
    table.add_column("Citation")
    table.add_column("Kind")
    table.add_column("Score")
    table.add_column("Snippet")
    for hit in payload["hits"]:
        snippet = " ".join(str(hit["snippet"]).splitlines()[:2]).strip()
        table.add_row(hit["citation"], hit["kind"], str(hit["score"]), snippet)
    return _render_rich(table)


def render_repair_payload(payload: dict[str, object]) -> str:
    body = [
        f"run: {payload['run_id']}",
        f"provider: {payload['provider']}",
        f"status: {payload['status']}",
    ]
    if payload["patch_id"]:
        body.append(f"patch: {payload['patch_id']}")
    if payload["proposal_explanation"]:
        body.append(f"proposal: {payload['proposal_explanation']}")
    if payload["validation_status"]:
        body.append(f"validation: {payload['validation_status']}")
        if payload.get("validation_mode"):
            body.append(f"validation mode: {payload['validation_mode']}")
        body.append(f"summary: {payload['validation_summary']}")
    return render_action_panel("Repair Result", "\n".join(body))


def render_agent_payload(payload: dict[str, object]) -> str:
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    body = [
        f"task: {payload['task_id']}",
        f"workflow: {payload['workflow_type']}",
        f"status: {payload['status']}",
        f"context: {payload['context_id']}",
    ]
    if isinstance(result, dict):
        for key in ("runtime_mode", "patch_id", "candidate_id", "validation_run_id", "validation_status", "validation_mode", "artifact_path"):
            value = result.get(key)
            if value:
                body.append(f"{key.replace('_', ' ')}: {value}")
        attempts = result.get("attempts")
        if isinstance(attempts, list) and attempts:
            body.append(f"attempts: {len(attempts)}")
            for item in attempts[:3]:
                if isinstance(item, dict):
                    body.append(f"- attempt {item.get('attempt')}: {item.get('status')} validation={item.get('validation_status')}")
        rejected = result.get("rejected_candidate_ids")
        if isinstance(rejected, list) and rejected:
            body.append(f"rejected candidates: {', '.join(str(item) for item in rejected[:3])}")
        summary = result.get("summary")
        if summary:
            body.append(f"summary: {summary}")
    if isinstance(evidence, dict) and evidence.get("replay_artifacts"):
        body.append("evidence: replay artifacts saved")
    review_gate = payload.get("review_gate")
    if isinstance(review_gate, dict):
        body.append(f"review: {review_gate.get('summary')}")
    if isinstance(result, dict):
        candidate_id = result.get("candidate_id")
        validation_run_id = result.get("validation_run_id")
        if candidate_id:
            body.append(f"next: /artifacts review {candidate_id}")
        elif validation_run_id:
            body.append(f"next: /runs show {validation_run_id}")
    return render_action_panel("Agent Result", "\n".join(body))


def render_triage_payload(payload: dict[str, object]) -> str:
    return render_action_panel("Triage Summary", format_triage_human(payload))


def render_coverage_payload(payload: dict[str, object]) -> str:
    return render_action_panel("Coverage Plan", format_coverage_human(payload))


def render_sva_payload(payload: dict[str, object]) -> str:
    body = [
        f"provider: {payload['provider']}",
        f"status: {payload['status']}",
        f"artifact: {payload['artifact_path']}",
        f"validation: {payload['validation_status']}",
        f"validation mode: {payload['validation_mode']}",
    ]
    if payload["explanation"]:
        body.append(f"explanation: {payload['explanation']}")
    attempts = payload.get("attempts") or []
    if isinstance(attempts, list) and attempts:
        body.append(f"attempts: {len(attempts)}")
    rejected = payload.get("rejected_candidate_ids") or []
    if isinstance(rejected, list) and rejected:
        body.append(f"rejected candidates: {', '.join(str(item) for item in rejected[:3])}")
    for item in payload["property_summaries"][:3]:
        body.append(f"property: {item['name']} -> {item['summary']}")
    return render_action_panel("Spec-to-SVA Result", "\n".join(body))


def render_cocotb_payload(payload: dict[str, object]) -> str:
    body = [
        f"provider: {payload['provider']}",
        f"status: {payload['status']}",
        f"top module: {payload['top_module']}",
        f"artifact: {payload['artifact_path']}",
        f"manifest: {payload['manifest_path']}",
        f"validation: {payload['validation_status']}",
        f"validation mode: {payload['validation_mode']}",
    ]
    if payload["explanation"]:
        body.append(f"explanation: {payload['explanation']}")
    attempts = payload.get("attempts") or []
    if isinstance(attempts, list) and attempts:
        body.append(f"attempts: {len(attempts)}")
    rejected = payload.get("rejected_candidate_ids") or []
    if isinstance(rejected, list) and rejected:
        body.append(f"rejected candidates: {', '.join(str(item) for item in rejected[:3])}")
    for assumption in payload.get("assumptions", [])[:3]:
        body.append(f"assumption: {assumption}")
    return render_action_panel("DUT-to-Cocotb Result", "\n".join(body))


def render_waveform_list_payload(payload: dict[str, object]) -> str:
    waveforms = payload["waveforms"]
    if not waveforms:
        return render_action_panel("Waveforms", "no waveform summaries recorded")
    table = Table(title="Waveforms", show_header=True, header_style="bold cyan")
    table.add_column("Waveform ID")
    table.add_column("Format")
    table.add_column("Timescale")
    table.add_column("Signals")
    table.add_column("Source")
    for item in waveforms[:10]:
        table.add_row(item["waveform_id"], item["format"], item["timescale"], str(len(item["signals"])), item["source_path"])
    return _render_rich(table)


def render_waveform_show_payload(payload: dict[str, object]) -> str:
    lines = [
        f"id: {payload['waveform_id']}",
        f"source: {payload['source_path']}",
        f"format: {payload['format']}",
        f"timescale: {payload['timescale']}",
        f"signals: {len(payload['signals'])}",
        f"scopes: {', '.join(payload['top_scopes']) or 'none'}",
    ]
    if payload.get("external_tool"):
        lines.append(f"external tool: {payload['external_tool']}")
    if payload.get("notes"):
        lines.append(f"notes: {payload['notes']}")
    return render_action_panel("Waveform Summary", "\n".join(lines))


def render_waveform_signals_payload(payload: dict[str, object]) -> str:
    table = Table(title=f"Signals {payload['waveform_id']}", show_header=True, header_style="bold cyan")
    table.add_column("Name")
    table.add_column("Full Name")
    table.add_column("Width")
    for item in payload["signals"][:20]:
        table.add_row(item["name"], item["full_name"], str(item["width"]))
    return _render_rich(table)


def render_waveform_inspect_payload(payload: dict[str, object]) -> str:
    body = [
        f"signal: {payload['full_name']}",
        f"timescale: {payload['timescale']}",
        f"transitions: {payload['transition_count']}",
    ]
    if payload["transitions"]:
        body.append("timeline:")
        body.extend(f"  {item['timestamp']}: {item['value']}" for item in payload["transitions"])
    return render_action_panel("Waveform Inspect", "\n".join(body))


def render_runs_payload(payload: list[dict[str, object]]) -> str:
    if not payload:
        return render_action_panel("Runs", "no runs recorded")
    table = Table(title="Recent Runs", show_header=True, header_style="bold cyan")
    table.add_column("Run ID")
    table.add_column("Workflow")
    table.add_column("Status")
    table.add_column("Tool")
    for run in payload[:10]:
        table.add_row(run["run_id"], run["workflow_type"], run["status"], run["tool"]["name"])
    return _render_rich(table)


def render_runs_doctor_payload(payload: dict[str, object]) -> str:
    issues = payload.get("issues") or []
    lines = [
        f"status: {payload['status']}",
        f"runs: {payload['run_count']}",
        f"load issues: {payload['issue_count']}",
    ]
    if isinstance(issues, list) and issues:
        lines.append("")
        for issue in issues[:5]:
            if isinstance(issue, dict):
                lines.append(f"- {issue.get('path')}: {issue.get('error')}")
    return render_action_panel("Runs Doctor", "\n".join(lines))


def render_import_runs_payload(payload: dict[str, object]) -> str:
    title = "Import Preview" if payload.get("dry_run") else "Runs Imported"
    lines = [
        f"manifest: {payload['manifest_path']}",
        f"runs: {payload['imported_count']}",
        f"dry run: {payload['dry_run']}",
    ]
    for item in payload.get("runs", [])[:5]:
        if isinstance(item, dict):
            lines.append(
                f"- {item.get('run_id')}: {item.get('name')} [{item.get('status')}], "
                f"observations={item.get('observation_count')}, waveforms={item.get('waveform_count')}"
            )
    return render_action_panel(title, "\n".join(lines))


def render_run_show(payload: dict[str, object]) -> str:
    lines = [
        f"run: {payload['run_id']}",
        f"workflow: {payload['workflow_type']}",
        f"status: {payload['status']}",
        f"tool: {payload['tool']['name']}",
        f"summary: {payload['summary']}",
    ]
    tool_result = payload.get("tool_result") or {}
    if isinstance(tool_result, dict) and tool_result:
        validation_mode = tool_result.get("validation_mode")
        if validation_mode:
            lines.append(f"validation mode: {validation_mode}")
        status = tool_result.get("status")
        if status:
            lines.append(f"tool result: {status}")
        report_source = tool_result.get("report_source")
        if report_source:
            lines.append(f"report source: {report_source}")
        recommendation_count = tool_result.get("recommendation_count")
        if recommendation_count is not None:
            lines.append(f"recommendations: {recommendation_count}")
        formal_run_id = tool_result.get("formal_run_id")
        if formal_run_id:
            lines.append(f"formal run: {formal_run_id}")
        top_module = tool_result.get("top_module")
        if top_module:
            lines.append(f"top module: {top_module}")
        runtime_mode = tool_result.get("runtime_mode")
        if runtime_mode:
            lines.append(f"runtime mode: {runtime_mode}")
        attempts = tool_result.get("attempts") or []
        if isinstance(attempts, list) and attempts:
            lines.append(f"attempts: {len(attempts)}")
            for item in attempts[:3]:
                if isinstance(item, dict):
                    lines.append(f"- attempt {item.get('attempt')}: {item.get('status')} validation={item.get('validation_status')}")
        rejected_candidate_ids = tool_result.get("rejected_candidate_ids") or []
        if isinstance(rejected_candidate_ids, list) and rejected_candidate_ids:
            lines.append(f"rejected candidates: {', '.join(str(item) for item in rejected_candidate_ids[:3])}")
        assumptions = tool_result.get("assumptions") or []
        if assumptions:
            lines.append(f"assumptions: {'; '.join(str(item) for item in assumptions[:2])}")
        classifications = tool_result.get("classifications") or []
        if classifications:
            lines.append(f"classifications: {', '.join(str(item) for item in classifications[:3])}")
        property_ids = tool_result.get("property_ids") or []
        if property_ids:
            lines.append(f"properties: {', '.join(property_ids[:4])}")
        counterexamples = tool_result.get("counterexample_paths") or []
        if counterexamples:
            lines.append(f"counterexamples: {', '.join(counterexamples[:2])}")
        report_paths = tool_result.get("report_paths") or []
        if report_paths:
            lines.append(f"reports: {', '.join(report_paths[:2])}")
    artifacts = payload.get("artifacts") or {}
    if isinstance(artifacts, dict) and artifacts:
        artifact_items = [f"{key}={value}" for key, value in list(artifacts.items())[:3]]
        lines.append(f"artifacts: {', '.join(artifact_items)}")
    return render_action_panel("Run Detail", "\n".join(lines))


def render_replay_payload(payload: dict[str, object]) -> str:
    if payload.get("status") == "confirmation_required":
        command = payload.get("replay_command") or []
        command_text = " ".join(str(part) for part in command) if isinstance(command, list) else str(command)
        return render_action_panel(
            "Replay Confirmation",
            f"run_id={payload.get('run_id')}\ncommand={command_text}\nnot executed; add --yes to run the stored command",
        )
    return render_action_panel(
        "Replay Output",
        f"exit_code={payload['exit_code']}\nstdout:\n{payload['stdout']}\nstderr:\n{payload['stderr']}",
    )


def render_eval_payload(payload: dict[str, object]) -> str:
    return render_action_panel("Evaluation", f"suite={payload['suite']}\npassed={payload['passed']}/{payload['total']}")


def render_action_panel(title: str, body: str) -> str:
    return _render_rich(Panel(body, title=title, border_style="cyan"))


def _render_intent(title: str, body: str) -> str:
    return render_action_panel(f"Inferred Intent: {title}", body)


def _header_fragments(session: ShellSession) -> list[tuple[str, str]]:
    project = session.project_config().project.name if session.project_config() else "no-project"
    cwd = _compact_path(session.cwd, max_chars=48)
    text = (
        f" Telchines | {project} | cwd: {cwd} | "
        f"repair: {_active_model_summary(session, 'repair')} | gen: {_active_model_summary(session, 'generation')} "
    )
    return [("class:header", text)]


def _sidebar_text(session: ShellSession) -> str:
    config = session.project_config()
    lines = [
        f"project: {config.project.name if config else 'none'}",
        f"cwd: {session.cwd.name}",
        f"index: {session.index_hint()}",
        f"repair: {_active_model_summary(session, 'repair')}",
        f"gen: {_active_model_summary(session, 'generation')}",
        f"last ctx: {session.last_context_id or 'none'}",
        f"logs: {session.logs_hint()}",
        "",
        "recent runs",
    ]
    if session.recent_run_ids:
        lines.extend(f"- {run_id}" for run_id in session.recent_run_ids)
    else:
        lines.append("- none")
    return "\n".join(lines)


def _active_model_summary(session: ShellSession, capability: str) -> str:
    config = session.project_config()
    if config is None:
        return "none"
    provider_name = config.default_provider_by_capability().get(capability, "heuristic")
    providers = config.project.model_policy.get("providers", {})
    provider_config = providers.get(provider_name)
    if not isinstance(provider_config, dict):
        return provider_name
    model = provider_config.get("model") or provider_config.get("base_provider") or ("heuristic" if provider_config.get("kind") == "heuristic" else "wrapper")
    reasoning = provider_config.get("reasoning_level", "auto")
    return f"{provider_name}:{model}/{reasoning}"


def _hint_fragments(session: ShellSession) -> list[tuple[str, str]]:
    hint = " /help for commands | Enter executes | Ctrl-C exits "
    return [("class:subtle", f" {hint}")]


def _parse_project_init(parts: list[str]) -> tuple[Path, str | None]:
    path = Path(".")
    name: str | None = None
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == "--name":
            name = _require_option_value(parts, index, "--name")
            index += 2
            continue
        path = Path(part)
        index += 1
    return path, name


def _parse_agent_args(parts: list[str]) -> dict[str, Any]:
    values: dict[str, Any] = {
        "task": "",
        "tool": None,
        "files": [],
        "extra_args": [],
        "adapter_args": [],
        "filelists": [],
        "include_dirs": [],
        "defines": [],
        "top_module": None,
        "work_library": None,
        "apply_patch": False,
        "logs": [],
        "waveforms": [],
        "report": None,
        "exclusions": None,
        "formal_run": None,
        "rtl": [],
        "spec": [],
        "dut": None,
        "output": None,
        "output_dir": None,
        "provider": None,
        "intent": "",
    }
    repeated = {
        "--file": "files",
        "--extra-arg": "extra_args",
        "--adapter-arg": "adapter_args",
        "--filelist": "filelists",
        "--include-dir": "include_dirs",
        "--define": "defines",
        "--logs": "logs",
        "--waveform": "waveforms",
        "--rtl": "rtl",
        "--spec": "spec",
    }
    single = {
        "--tool": "tool",
        "--report": "report",
        "--exclusions": "exclusions",
        "--formal-run": "formal_run",
        "--dut": "dut",
        "--output": "output",
        "--output-dir": "output_dir",
        "--provider": "provider",
        "--intent": "intent",
        "--top": "top_module",
        "--worklib": "work_library",
    }
    task_parts: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == "--apply":
            values["apply_patch"] = True
            index += 1
            continue
        if part in repeated:
            casted = values[repeated[part]]
            if isinstance(casted, list):
                casted.append(_require_option_value(parts, index, part))
            index += 2
            continue
        if part in single:
            values[single[part]] = _require_option_value(parts, index, part)
            index += 2
            continue
        if part.startswith("--"):
            raise ValueError(f"unrecognized agent argument: {part}")
        task_parts.append(part)
        index += 1
    task = " ".join(task_parts).strip()
    if not task:
        raise ValueError("/agent requires a natural-language task")
    values["task"] = task
    return values


def _empty_compile_context() -> dict[str, Any]:
    return {
        "adapter_args": [],
        "filelists": [],
        "include_dirs": [],
        "defines": [],
        "top_module": None,
        "work_library": None,
    }


def _parse_compile_context_option(parts: list[str], index: int, context: dict[str, Any]) -> int | None:
    part = parts[index]
    repeated = {
        "--adapter-arg": "adapter_args",
        "--filelist": "filelists",
        "--include-dir": "include_dirs",
        "--define": "defines",
    }
    single = {"--top": "top_module", "--worklib": "work_library"}
    if part in repeated:
        values = context[repeated[part]]
        if isinstance(values, list):
            values.append(_require_option_value(parts, index, part))
        return index + 2
    if part in single:
        context[single[part]] = _require_option_value(parts, index, part)
        return index + 2
    return None


def _parse_repair_args(parts: list[str]) -> tuple[str, list[str], list[str], bool, dict[str, Any]]:
    tool: str | None = None
    files: list[str] = []
    extra_args: list[str] = []
    context = _empty_compile_context()
    apply_patch = False
    index = 0
    while index < len(parts):
        part = parts[index]
        next_index = _parse_compile_context_option(parts, index, context)
        if next_index is not None:
            index = next_index
            continue
        if part == "--tool":
            tool = _require_option_value(parts, index, "--tool")
            index += 2
            continue
        if part == "--file":
            files.append(_require_option_value(parts, index, "--file"))
            index += 2
            continue
        if part == "--extra-arg":
            extra_args.append(_require_option_value(parts, index, "--extra-arg"))
            index += 2
            continue
        if part == "--apply":
            apply_patch = True
            index += 1
            continue
        raise ValueError(f"unrecognized repair argument: {part}")
    if not tool:
        raise ValueError("/repair requires --tool")
    if not files and not context["filelists"]:
        raise ValueError("/repair requires at least one --file or --filelist")
    return tool, files, extra_args, apply_patch, context


def _parse_gen_sva_args(parts: list[str]) -> tuple[str, str, str | None, str | None, dict[str, Any]]:
    spec: str | None = None
    rtl: str | None = None
    output: str | None = None
    provider: str | None = None
    context = _empty_compile_context()
    index = 0
    while index < len(parts):
        part = parts[index]
        next_index = _parse_compile_context_option(parts, index, context)
        if next_index is not None:
            index = next_index
            continue
        if part == "--spec":
            spec = _require_option_value(parts, index, "--spec")
            index += 2
            continue
        if part == "--rtl":
            rtl = _require_option_value(parts, index, "--rtl")
            index += 2
            continue
        if part == "--output":
            output = _require_option_value(parts, index, "--output")
            index += 2
            continue
        if part == "--provider":
            provider = _require_option_value(parts, index, "--provider")
            index += 2
            continue
        raise ValueError(f"unrecognized gen-sva argument: {part}")
    if not spec:
        raise ValueError("/gen-sva requires --spec")
    if not rtl:
        raise ValueError("/gen-sva requires --rtl")
    return spec, rtl, output, provider, context


def _parse_gen_cocotb_args(parts: list[str]) -> tuple[str, str | None, str | None, str, str | None, dict[str, Any]]:
    dut: str | None = None
    spec: str | None = None
    output_dir: str | None = None
    intent = ""
    provider: str | None = None
    context = _empty_compile_context()
    index = 0
    while index < len(parts):
        part = parts[index]
        next_index = _parse_compile_context_option(parts, index, context)
        if next_index is not None:
            index = next_index
            continue
        if part == "--dut":
            dut = _require_option_value(parts, index, "--dut")
            index += 2
            continue
        if part == "--spec":
            spec = _require_option_value(parts, index, "--spec")
            index += 2
            continue
        if part == "--output-dir":
            output_dir = _require_option_value(parts, index, "--output-dir")
            index += 2
            continue
        if part == "--intent":
            intent = _require_option_value(parts, index, "--intent")
            index += 2
            continue
        if part == "--provider":
            provider = _require_option_value(parts, index, "--provider")
            index += 2
            continue
        raise ValueError(f"unrecognized gen-cocotb argument: {part}")
    if not dut:
        raise ValueError("/gen-cocotb requires --dut")
    return dut, spec, output_dir, intent, provider, context


def _parse_coverage_plan_args(parts: list[str]) -> tuple[str, str | None, str | None, list[str], list[str]]:
    report: str | None = None
    exclusions: str | None = None
    formal_run: str | None = None
    rtl_paths: list[str] = []
    spec_paths: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == "--report":
            report = _require_option_value(parts, index, "--report")
            index += 2
            continue
        if part == "--exclusions":
            exclusions = _require_option_value(parts, index, "--exclusions")
            index += 2
            continue
        if part == "--formal-run":
            formal_run = _require_option_value(parts, index, "--formal-run")
            index += 2
            continue
        if part == "--rtl":
            rtl_paths.append(_require_option_value(parts, index, "--rtl"))
            index += 2
            continue
        if part == "--spec":
            spec_paths.append(_require_option_value(parts, index, "--spec"))
            index += 2
            continue
        raise ValueError(f"unrecognized coverage-plan argument: {part}")
    if not report:
        raise ValueError("/coverage-plan requires --report")
    return report, exclusions, formal_run, rtl_paths, spec_paths


def _parse_repeated_option(parts: list[str], option_name: str, strict: bool = True) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(parts):
        if parts[index] == option_name:
            values.append(_require_option_value(parts, index, option_name))
            index += 2
            continue
        if strict:
            raise ValueError(f"unrecognized argument: {parts[index]}")
        index += 1
    return values


def _parse_required_argument(parts: list[str], option_name: str) -> str:
    value = _parse_optional_argument(parts, option_name)
    if value is None:
        raise ValueError(f"{option_name} requires a value")
    return value


def _parse_optional_argument(parts: list[str], option_name: str) -> str | None:
    index = 0
    while index < len(parts):
        if parts[index] == option_name:
            return _require_option_value(parts, index, option_name)
        index += 1
    return None


def _require_option_value(parts: list[str], index: int, option_name: str) -> str:
    if index + 1 >= len(parts) or parts[index + 1].startswith("--"):
        raise ValueError(f"{option_name} requires a value")
    return parts[index + 1]


def _resolve_path(cwd: Path, value: str) -> Path:
    path = Path(value)
    return (cwd / path).resolve() if not path.is_absolute() else path.resolve()


def _default_logs_path(cwd: Path) -> Path | None:
    candidates = [cwd / "logs" / "regressions", cwd / "logs"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _render_rich(renderable, width: int = 100) -> str:
    console = Console(record=True, width=width, soft_wrap=True)
    console.print(renderable)
    return console.export_text(styles=False).rstrip()


def _completing_command_name(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("/") and " " not in stripped


def _option_completion_seed(text: str) -> str | None:
    if not text.startswith("/"):
        return None
    if text.endswith(" "):
        return ""
    current = text.rsplit(maxsplit=1)[-1]
    if current.startswith("--"):
        return current
    return None


def _option_completions(text: str, seed: str) -> list[str]:
    try:
        parts = shlex.split(text)
    except ValueError:
        return []
    if not parts:
        return []
    if seed and parts[-1] == seed:
        parts = parts[:-1]
    key = _option_command_key(parts)
    options = SHELL_COMMAND_OPTIONS.get(key, ())
    used = {part for part in parts if part.startswith("--")}
    return [option for option in options if option.startswith(seed) and (option in REPEATABLE_OPTIONS or option not in used)]


def _option_command_key(parts: list[str]) -> str:
    first = parts[0] if parts else ""
    command = first if first.startswith("/") else f"/{first}"
    if command in {"/project", "/providers", "/runs", "/waveforms", "/artifacts"} and len(parts) > 1 and not parts[1].startswith("--"):
        return f"{command} {parts[1]}"
    return command


def _path_completion_seed(text: str) -> str | None:
    if not text or text.endswith(" "):
        parts = shlex.split(text)
        previous = parts[-1] if parts else ""
        if previous in PATH_OPTIONS or previous == "/cd":
            return ""
        return None
    try:
        parts = shlex.split(text)
    except ValueError:
        return None
    if len(parts) >= 2 and (parts[-2] in PATH_OPTIONS or parts[0] == "/cd"):
        return parts[-1]
    return None


def _file_mention_seed(text: str) -> str | None:
    token = text.rsplit(maxsplit=1)[-1] if text.strip() else ""
    if token.startswith("@"):
        return token[1:]
    return None


def _file_mention_completions(cwd: Path, seed: str) -> list[str]:
    completions: list[str] = []
    excluded = {".git", ".tel", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache"}
    for path in sorted(cwd.rglob("*"), key=lambda item: item.as_posix().lower()):
        try:
            relative = path.relative_to(cwd)
        except ValueError:
            continue
        if any(part in excluded for part in relative.parts):
            continue
        if not path.is_file():
            continue
        text = relative.as_posix()
        if text.startswith(seed):
            completions.append(f"@{text}")
    return completions[:25]


def _path_completions(cwd: Path, seed: str) -> list[str]:
    seed_path = Path(seed)
    base = seed_path.parent if str(seed_path.parent) != "." else Path(".")
    directory = (cwd / base).resolve() if not base.is_absolute() else base.resolve()
    prefix = seed_path.name
    if not directory.exists() or not directory.is_dir():
        return []
    completions: list[str] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
        if not path.name.startswith(prefix):
            continue
        rel = path.relative_to(cwd) if path.is_relative_to(cwd) else path
        suffix = "/" if path.is_dir() else ""
        completions.append(rel.as_posix() + suffix)
    return completions[:25]


def _compact_path(path: Path, max_chars: int = 48) -> str:
    text = str(path)
    if len(text) <= max_chars:
        return text
    tail = path.name
    parent = path.parent.name
    compact = f"...{os.sep}{parent}{os.sep}{tail}" if parent else f"...{os.sep}{tail}"
    return compact if len(compact) <= max_chars else "..." + text[-max_chars + 3 :]
