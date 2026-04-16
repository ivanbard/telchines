from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

import typer
from prompt_toolkit import Application
from prompt_toolkit.filters import Condition
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
    coverage_plan,
    dump_json,
    format_coverage_human,
    format_triage_human,
    gen_cocotb,
    gen_sva,
    inspect_waveform,
    index_project,
    initialize_project,
    list_providers,
    list_runs,
    list_waveforms,
    load_eval_report,
    repair,
    replay_run,
    retrieve_query,
    run_eval,
    show_run,
    show_waveform,
    triage,
    waveform_signals,
)


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

    def logs_hint(self) -> str:
        candidate = _default_logs_path(self.cwd)
        return str(candidate.relative_to(self.cwd)) if candidate and candidate.exists() else "none"


@dataclass(slots=True)
class ShellViewState:
    help_visible: bool = False
    help_text: str = ""
    saved_input_text: str = ""
    saved_cursor_position: int = 0


def run_shell(initial_cwd: Path | None = None) -> None:
    session = ShellSession(cwd=(initial_cwd or Path.cwd()).resolve())
    session.add_transcript("Telchines", render_welcome(session))
    if _supports_fullscreen_shell():
        _run_fullscreen_shell(session)
        return
    _run_basic_shell(session)


def _supports_fullscreen_shell() -> bool:
    if os.environ.get("TELCHINES_PLAIN_SHELL") == "1":
        return False
    stdin = getattr(sys.stdin, "isatty", lambda: False)()
    stdout = getattr(sys.stdout, "isatty", lambda: False)()
    return bool(stdin and stdout)


def _run_basic_shell(session: ShellSession) -> None:
    typer.echo("Telchines interactive shell.")
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
        try:
            should_exit, rendered = dispatch_input(session, user_input)
        except (ConfigError, ProviderError, AdapterExecutionError, ValueError, KeyError) as exc:
            typer.echo(f"error: {exc}")
            continue
        except TelchinesError as exc:
            typer.echo(f"error: {exc}")
            continue
        if rendered:
            typer.echo(rendered)
        if should_exit:
            return


def _run_fullscreen_shell(session: ShellSession) -> None:
    view_state = ShellViewState()
    transcript_area = TextArea(
        text="\n\n".join(session.transcript),
        read_only=True,
        scrollbar=True,
        focusable=False,
        wrap_lines=True,
    )
    input_area = TextArea(
        height=1,
        prompt=session.prompt(),
        multiline=False,
        wrap_lines=False,
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
            Frame(sidebar_window, title="Status", width=Dimension(preferred=28, max=32)),
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
        if _is_help_command(user_input):
            show_help_overlay()
            input_area.text = ""
            input_area.prompt = session.prompt()
            return
        session.transcript.append(f"{session.prompt()}{user_input}")
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

    app = Application(layout=layout, key_bindings=kb, full_screen=True, mouse_support=False, style=style)
    app.layout.focus(input_area)
    app.run()


def dispatch_input(session: ShellSession, user_input: str) -> tuple[bool, str]:
    if user_input.startswith("/"):
        return _dispatch_slash_command(session, user_input[1:])
    return _dispatch_plain_text(session, user_input)


def _dispatch_slash_command(session: ShellSession, command_line: str) -> tuple[bool, str]:
    parts = shlex.split(command_line)
    if not parts:
        return False, ""
    command = parts[0].lower()

    if command in {"exit", "quit"}:
        return True, "leaving Telchines shell"
    if command == "help":
        return False, render_help()
    if command == "pwd":
        return False, str(session.cwd)
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

    payload = _execute_command(session, parts, raw=False)
    return False, payload or ""


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
        chunk_count = index_project(session.cwd)
        payload = {"indexed_chunks": chunk_count}
        return dump_json(payload) if raw else render_action_panel("Index Complete", f"indexed {chunk_count} chunks")

    if command == "retrieve":
        query = " ".join(parts[1:]).strip()
        if not query:
            raise ValueError("/retrieve requires a query")
        payload = retrieve_query(session.cwd, query, mode="general")
        session.note_context(payload)
        return dump_json(payload) if raw else render_retrieval_payload(payload)

    if command == "providers":
        payload = list_providers(session.cwd)
        return dump_json(payload) if raw else render_provider_payload(payload)

    if command == "repair":
        tool, files, extra_args, apply_patch = _parse_repair_args(parts[1:])
        payload = repair(session.cwd, tool=tool, files=files, extra_arg=extra_args, apply_patch=apply_patch)
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
        spec, rtl, output, provider = _parse_gen_sva_args(parts[1:])
        payload = gen_sva(session.cwd, spec=_resolve_path(session.cwd, spec), rtl=_resolve_path(session.cwd, rtl), output=None if output is None else _resolve_path(session.cwd, output), provider_name=provider)
        session.note_context(payload)
        return dump_json(payload) if raw else render_sva_payload(payload)

    if command == "gen-cocotb":
        dut, spec, output_dir, intent, provider = _parse_gen_cocotb_args(parts[1:])
        payload = gen_cocotb(
            session.cwd,
            dut=_resolve_path(session.cwd, dut),
            spec=None if spec is None else _resolve_path(session.cwd, spec),
            output_dir=None if output_dir is None else _resolve_path(session.cwd, output_dir),
            intent=intent,
            provider_name=provider,
        )
        session.note_context(payload)
        return dump_json(payload) if raw else render_cocotb_payload(payload)

    if command == "runs":
        if len(parts) == 1 or parts[1] == "list":
            payload = list_runs(session.cwd)
            return dump_json(payload) if raw else render_runs_payload(payload)
        if parts[1] == "show" and len(parts) > 2:
            payload = show_run(session.cwd, parts[2])
            return dump_json(payload) if raw else render_run_show(payload)
        if parts[1] == "replay" and len(parts) > 2:
            payload = replay_run(session.cwd, parts[2])
            return dump_json(payload) if raw else render_replay_payload(payload)
        raise ValueError("supported /runs commands are list, show <run_id>, and replay <run_id>")

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
    commands = [
        ("/help", "Show command reference"),
        ("/project init [path] [--name NAME]", "Initialize a Telchines project"),
        ("/index", "Build retrieval index"),
        ("/retrieve QUERY", "Search project context"),
        ("/providers", "Show configured providers and policy status"),
        ("/repair --tool TOOL --file PATH", "Run repair workflow"),
        ("/triage --logs PATH [--logs PATH] [--waveform PATH]", "Run regression triage"),
        ("/coverage-plan --report PATH [--exclusions PATH] [--formal-run RUN_ID]", "Generate coverage closure recommendations"),
        ("/gen-sva --spec PATH --rtl PATH [--output PATH]", "Generate assertion draft from spec and RTL"),
        ("/gen-cocotb --dut PATH [--spec PATH] [--output-dir PATH]", "Generate a cocotb scaffold from DUT context"),
        ("/waveforms [list|show TARGET|signals TARGET|inspect TARGET --signal NAME]", "Inspect waveform summaries and signals"),
        ("/runs [list|show RUN_ID|replay RUN_ID]", "Inspect stored runs"),
        ("/eval [run|report]", "Run or show benchmarks"),
        ("/cd PATH", "Change working directory"),
        ("/pwd", "Show current working directory"),
        ("/raw <slash command>", "Display raw JSON output"),
        ("/exit", "Leave the shell"),
    ]
    for command, purpose in commands:
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
    table.add_column("Status")
    for provider in payload["providers"]:
        status = "allowed" if provider["allowed"] else f"blocked: {provider['blocked_reason']}"
        table.add_row(
            provider["name"],
            provider["kind"],
            ", ".join(provider["capabilities"]),
            ", ".join(provider["default_for"]) or "none",
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
        body.append(f"summary: {payload['validation_summary']}")
    return render_action_panel("Repair Result", "\n".join(body))


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
    ]
    if payload["explanation"]:
        body.append(f"explanation: {payload['explanation']}")
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
    ]
    if payload["explanation"]:
        body.append(f"explanation: {payload['explanation']}")
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
    text = (
        f" Telchines | {project} | cwd: {session.cwd} | "
        f"repair: {session.active_provider()} | gen: {session.active_generation_provider()} "
    )
    return [("class:header", text)]


def _sidebar_text(session: ShellSession) -> str:
    config = session.project_config()
    lines = [
        f"project: {config.project.name if config else 'none'}",
        f"cwd: {session.cwd.name}",
        f"indexed: {'yes' if session.indexed() else 'no'}",
        f"repair: {session.active_provider()}",
        f"gen: {session.active_generation_provider()}",
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
            if index + 1 >= len(parts):
                raise ValueError("--name requires a value")
            name = parts[index + 1]
            index += 2
            continue
        path = Path(part)
        index += 1
    return path, name


def _parse_repair_args(parts: list[str]) -> tuple[str, list[str], list[str], bool]:
    tool: str | None = None
    files: list[str] = []
    extra_args: list[str] = []
    apply_patch = False
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == "--tool":
            tool = parts[index + 1]
            index += 2
            continue
        if part == "--file":
            files.append(parts[index + 1])
            index += 2
            continue
        if part == "--extra-arg":
            extra_args.append(parts[index + 1])
            index += 2
            continue
        if part == "--apply":
            apply_patch = True
            index += 1
            continue
        raise ValueError(f"unrecognized repair argument: {part}")
    if not tool:
        raise ValueError("/repair requires --tool")
    if not files:
        raise ValueError("/repair requires at least one --file")
    return tool, files, extra_args, apply_patch


def _parse_gen_sva_args(parts: list[str]) -> tuple[str, str, str | None, str | None]:
    spec: str | None = None
    rtl: str | None = None
    output: str | None = None
    provider: str | None = None
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == "--spec":
            spec = parts[index + 1]
            index += 2
            continue
        if part == "--rtl":
            rtl = parts[index + 1]
            index += 2
            continue
        if part == "--output":
            output = parts[index + 1]
            index += 2
            continue
        if part == "--provider":
            provider = parts[index + 1]
            index += 2
            continue
        raise ValueError(f"unrecognized gen-sva argument: {part}")
    if not spec:
        raise ValueError("/gen-sva requires --spec")
    if not rtl:
        raise ValueError("/gen-sva requires --rtl")
    return spec, rtl, output, provider


def _parse_gen_cocotb_args(parts: list[str]) -> tuple[str, str | None, str | None, str, str | None]:
    dut: str | None = None
    spec: str | None = None
    output_dir: str | None = None
    intent = ""
    provider: str | None = None
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == "--dut":
            dut = parts[index + 1]
            index += 2
            continue
        if part == "--spec":
            spec = parts[index + 1]
            index += 2
            continue
        if part == "--output-dir":
            output_dir = parts[index + 1]
            index += 2
            continue
        if part == "--intent":
            intent = parts[index + 1]
            index += 2
            continue
        if part == "--provider":
            provider = parts[index + 1]
            index += 2
            continue
        raise ValueError(f"unrecognized gen-cocotb argument: {part}")
    if not dut:
        raise ValueError("/gen-cocotb requires --dut")
    return dut, spec, output_dir, intent, provider


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
            report = parts[index + 1]
            index += 2
            continue
        if part == "--exclusions":
            exclusions = parts[index + 1]
            index += 2
            continue
        if part == "--formal-run":
            formal_run = parts[index + 1]
            index += 2
            continue
        if part == "--rtl":
            rtl_paths.append(parts[index + 1])
            index += 2
            continue
        if part == "--spec":
            spec_paths.append(parts[index + 1])
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
            if index + 1 >= len(parts):
                raise ValueError(f"{option_name} requires a value")
            values.append(parts[index + 1])
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
            if index + 1 >= len(parts):
                raise ValueError(f"{option_name} requires a value")
            return parts[index + 1]
        index += 1
    return None


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
