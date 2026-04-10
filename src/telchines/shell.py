from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

import typer
from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, FormattedTextControl, HSplit, Layout, VSplit, Window
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from telchines.config import ProjectConfig
from telchines.errors import AdapterExecutionError, ConfigError, ProviderError, TelchinesError
from telchines.operations import (
    dump_json,
    format_triage_human,
    index_project,
    initialize_project,
    list_providers,
    list_runs,
    load_eval_report,
    repair,
    replay_run,
    retrieve_query,
    run_eval,
    show_run,
    triage,
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

    header_window = Window(height=3, content=FormattedTextControl(text=lambda: _header_fragments(session)))
    sidebar_window = Window(content=FormattedTextControl(text=lambda: _sidebar_text(session)), wrap_lines=True)
    hint_window = Window(height=2, content=FormattedTextControl(text=lambda: _hint_fragments(session)))

    layout = Layout(
        HSplit(
            [
                header_window,
                VSplit(
                    [
                        Frame(sidebar_window, title="Verification Context", width=Dimension(preferred=38)),
                        Frame(transcript_area, title="Transcript"),
                    ]
                ),
                Frame(hint_window, title="Discover"),
                Frame(input_area, title="Command Input"),
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

    def submit() -> None:
        user_input = input_area.text.strip()
        if not user_input:
            return
        session.history.append(user_input)
        session.transcript.append(f"> {user_input}")
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

    @kb.add("c-c")
    @kb.add("c-d")
    def _(event) -> None:  # noqa: ANN001
        session.transcript.append("leaving Telchines shell")
        transcript_area.text = "\n\n".join(session.transcript)
        event.app.exit()

    app = Application(layout=layout, key_bindings=kb, full_screen=True, mouse_support=False, style=style)
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
    if any(keyword in lowered for keyword in ("retrieve", "search", "find")):
        payload = retrieve_query(session.cwd, user_input, mode="general")
        session.note_context(payload)
        return False, _render_intent("Retrieve project context", render_retrieval_payload(payload))
    if "run" in lowered:
        return False, _render_intent("Inspect recent runs", render_runs_payload(list_runs(session.cwd)))
    if "repair" in lowered:
        return False, "I can run repair, but I need an explicit tool and file. Try `/repair --tool verilator --file rtl/foo.sv`."
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
        logs = _parse_repeated_option(parts[1:], "--logs")
        if not logs:
            raise ValueError("/triage requires at least one --logs path")
        payload = triage(session.cwd, [_resolve_path(session.cwd, value) for value in logs])
        session.note_context(payload)
        return dump_json(payload) if raw else render_triage_payload(payload)

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

    raise ValueError(f"unknown slash command: /{command}")


def render_welcome(session: ShellSession) -> str:
    config = session.project_config()
    table = Table.grid(padding=(0, 1))
    table.add_column(style="cyan", justify="right")
    table.add_column(style="white")
    table.add_row("Project", config.project.name if config else "No Telchines project detected")
    table.add_row("CWD", str(session.cwd))
    table.add_row("Repair Provider", session.active_provider())
    table.add_row("Indexed", "yes" if session.indexed() else "no")
    table.add_row("Logs Hint", session.logs_hint())
    table.add_row("Try", "/help, /providers, /index, /triage --logs logs/regressions")
    return _render_rich(
        Panel(
            table,
            title="Telchines Verification Cockpit",
            subtitle="Full-screen interactive shell",
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
        ("/triage --logs PATH [--logs PATH]", "Run regression triage"),
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
    return render_action_panel(
        "Run Detail",
        "\n".join(
            [
                f"run: {payload['run_id']}",
                f"workflow: {payload['workflow_type']}",
                f"status: {payload['status']}",
                f"tool: {payload['tool']['name']}",
                f"summary: {payload['summary']}",
            ]
        ),
    )


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
    text = f" Telchines  |  Project: {project}  |  CWD: {session.cwd}  |  Repair Provider: {session.active_provider()} "
    return [("class:header", text)]


def _sidebar_text(session: ShellSession) -> str:
    lines = [
        "Status",
        f"project: {session.project_config().project.name if session.project_config() else 'none'}",
        f"indexed: {'yes' if session.indexed() else 'no'}",
        f"logs: {session.logs_hint()}",
        f"last ctx: {session.last_context_id or 'none'}",
        "",
        "Recent Runs",
    ]
    if session.recent_run_ids:
        lines.extend(f"- {run_id}" for run_id in session.recent_run_ids)
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Quick Actions",
            "- /help",
            "- /providers",
            "- /index",
            "- /triage --logs logs/regressions",
            "- /repair --tool verilator --file rtl/foo.sv",
        ]
    )
    return "\n".join(lines)


def _hint_fragments(session: ShellSession) -> list[tuple[str, str]]:
    hint = "Slash commands are the reliable path. Plain text can infer providers, indexing, retrieval, triage, and runs."
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


def _parse_repeated_option(parts: list[str], option_name: str) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(parts):
        if parts[index] != option_name:
            raise ValueError(f"unrecognized argument: {parts[index]}")
        if index + 1 >= len(parts):
            raise ValueError(f"{option_name} requires a value")
        values.append(parts[index + 1])
        index += 2
    return values


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
