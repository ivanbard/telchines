from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

import typer

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

    def prompt(self) -> str:
        try:
            config = ProjectConfig.discover(self.cwd)
            project_name = config.project.name
            return f"tel[{project_name}] {self.cwd.name}> "
        except ConfigError:
            return f"tel {self.cwd.name}> "

    def note_context(self, payload: dict[str, object]) -> None:
        context_id = payload.get("context_id")
        if isinstance(context_id, str) and context_id:
            self.last_context_id = context_id
        for key in ("run_id", "validation_run_id"):
            run_id = payload.get(key)
            if isinstance(run_id, str) and run_id and run_id not in self.recent_run_ids:
                self.recent_run_ids.insert(0, run_id)
        self.recent_run_ids = self.recent_run_ids[:5]


def run_shell(initial_cwd: Path | None = None) -> None:
    session = ShellSession(cwd=(initial_cwd or Path.cwd()).resolve())
    typer.echo("Telchines interactive shell. Type /help for commands, /exit to leave.")
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
            should_exit = _dispatch_input(session, user_input)
        except (ConfigError, ProviderError, AdapterExecutionError, ValueError, KeyError) as exc:
            typer.echo(f"error: {exc}")
            continue
        except TelchinesError as exc:
            typer.echo(f"error: {exc}")
            continue
        if should_exit:
            return


def _dispatch_input(session: ShellSession, user_input: str) -> bool:
    if user_input.startswith("/"):
        return _dispatch_slash_command(session, user_input[1:])
    return _dispatch_plain_text(session, user_input)


def _dispatch_slash_command(session: ShellSession, command_line: str) -> bool:
    parts = shlex.split(command_line)
    if not parts:
        return False
    command = parts[0].lower()

    if command in {"exit", "quit"}:
        typer.echo("leaving Telchines shell")
        return True
    if command == "help":
        typer.echo(_help_text())
        return False
    if command == "pwd":
        typer.echo(str(session.cwd))
        return False
    if command == "cd":
        target = Path(parts[1]) if len(parts) > 1 else Path.home()
        resolved = (session.cwd / target).resolve() if not target.is_absolute() else target.resolve()
        if not resolved.exists() or not resolved.is_dir():
            raise ValueError(f"directory does not exist: {resolved}")
        session.cwd = resolved
        typer.echo(str(session.cwd))
        return False
    if command == "raw":
        if len(parts) < 2:
            raise ValueError("/raw requires a slash command payload")
        payload = _execute_command(session, parts[1:], raw=True)
        if payload is not None:
            typer.echo(payload)
        return False

    payload = _execute_command(session, parts, raw=False)
    if payload is not None:
        typer.echo(payload)
    return False


def _dispatch_plain_text(session: ShellSession, user_input: str) -> bool:
    lowered = user_input.lower()
    if "provider" in lowered:
        typer.echo(_format_provider_payload(list_providers(session.cwd)))
        return False
    if "index" in lowered:
        chunk_count = index_project(session.cwd)
        typer.echo(f"indexed {chunk_count} chunks")
        return False
    if "triage" in lowered:
        logs_path = _default_logs_path(session.cwd)
        if logs_path is None:
            typer.echo("I can run triage, but I need a log path. Try `/triage --logs path/to/logs`.")
            return False
        payload = triage(session.cwd, [logs_path])
        session.note_context(payload)
        typer.echo(format_triage_human(payload))
        return False
    if any(keyword in lowered for keyword in ("retrieve", "search", "find")):
        payload = retrieve_query(session.cwd, user_input, mode="general")
        session.note_context(payload)
        typer.echo(_format_retrieval_payload(payload))
        return False
    if "run" in lowered:
        typer.echo(_format_runs_payload(list_runs(session.cwd)))
        return False
    if "repair" in lowered:
        typer.echo("I can run repair, but I need an explicit tool and file. Try `/repair --tool verilator --file rtl/foo.sv`.")
        return False
    typer.echo("I did not recognize that request. Use /help or try a command like `/providers`, `/index`, or `/triage --logs logs/regressions`.")
    return False


def _execute_command(session: ShellSession, parts: list[str], raw: bool) -> str | None:
    command = parts[0].lower()

    if command == "project" and len(parts) > 1 and parts[1] == "init":
        path, name = _parse_project_init(parts[2:])
        config = initialize_project((session.cwd / path).resolve() if not path.is_absolute() else path.resolve(), name=name)
        session.cwd = config.project_root
        return dump_json({"project_id": config.project.project_id, "root_path": config.project.root_path}) if raw else f"initialized project {config.project.project_id} at {config.project.root_path}"

    if command == "index":
        chunk_count = index_project(session.cwd)
        return dump_json({"indexed_chunks": chunk_count}) if raw else f"indexed {chunk_count} chunks"

    if command == "retrieve":
        query = " ".join(parts[1:]).strip()
        if not query:
            raise ValueError("/retrieve requires a query")
        payload = retrieve_query(session.cwd, query, mode="general")
        session.note_context(payload)
        return dump_json(payload) if raw else _format_retrieval_payload(payload)

    if command == "providers":
        payload = list_providers(session.cwd)
        return dump_json(payload) if raw else _format_provider_payload(payload)

    if command == "repair":
        tool, files, extra_args, apply_patch = _parse_repair_args(parts[1:])
        payload = repair(session.cwd, tool=tool, files=files, extra_arg=extra_args, apply_patch=apply_patch)
        session.note_context(payload)
        return dump_json(payload) if raw else _format_repair_payload(payload)

    if command == "triage":
        logs = _parse_repeated_option(parts[1:], "--logs")
        if not logs:
            raise ValueError("/triage requires at least one --logs path")
        payload = triage(session.cwd, [_resolve_path(session.cwd, value) for value in logs])
        session.note_context(payload)
        return dump_json(payload) if raw else format_triage_human(payload)

    if command == "runs":
        if len(parts) == 1 or parts[1] == "list":
            payload = list_runs(session.cwd)
            return dump_json(payload) if raw else _format_runs_payload(payload)
        if parts[1] == "show" and len(parts) > 2:
            payload = show_run(session.cwd, parts[2])
            return dump_json(payload) if raw else _format_run_show(payload)
        if parts[1] == "replay" and len(parts) > 2:
            payload = replay_run(session.cwd, parts[2])
            return dump_json(payload) if raw else _format_replay_payload(payload)
        raise ValueError("supported /runs commands are list, show <run_id>, and replay <run_id>")

    if command == "eval":
        if len(parts) == 1 or parts[1] == "run":
            payload = run_eval(session.cwd)
            return dump_json(payload) if raw else _format_eval_payload(payload)
        if parts[1] == "report":
            payload = load_eval_report(session.cwd)
            return dump_json(payload) if raw else _format_eval_payload(payload)
        raise ValueError("supported /eval commands are run and report")

    raise ValueError(f"unknown slash command: /{command}")


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


def _help_text() -> str:
    return "\n".join(
        [
            "Telchines shell commands:",
            "/help",
            "/project init [path] [--name NAME]",
            "/index",
            "/retrieve QUERY",
            "/providers",
            "/repair --tool TOOL --file PATH [--file PATH] [--extra-arg ARG] [--apply]",
            "/triage --logs PATH [--logs PATH]",
            "/runs [list|show RUN_ID|replay RUN_ID]",
            "/eval [run|report]",
            "/cd PATH",
            "/pwd",
            "/raw <slash command>",
            "/exit",
        ]
    )


def _format_retrieval_payload(payload: dict[str, object]) -> str:
    lines = [f"context {payload['context_id']} ({payload['mode']}) returned {len(payload['hits'])} hit(s)"]
    for hit in payload["hits"]:
        snippet = " ".join(str(hit["snippet"]).splitlines()[:1]).strip()
        lines.append(f"- {hit['citation']} [{hit['kind']}] score={hit['score']}: {snippet}")
    return "\n".join(lines)


def _format_provider_payload(payload: dict[str, object]) -> str:
    lines = [f"default providers: {payload['default_provider_by_capability']}"]
    for provider in payload["providers"]:
        default_for = ", ".join(provider["default_for"]) or "none"
        status = "allowed" if provider["allowed"] else f"blocked ({provider['blocked_reason']})"
        lines.append(
            f"- {provider['name']} [{provider['kind']}] capabilities={','.join(provider['capabilities'])} default_for={default_for} {status}"
        )
    return "\n".join(lines)


def _format_repair_payload(payload: dict[str, object]) -> str:
    lines = [
        f"repair run {payload['run_id']} status={payload['status']}",
        f"provider: {payload['provider']}",
    ]
    if payload["patch_id"]:
        lines.append(f"patch: {payload['patch_id']}")
    if payload["proposal_explanation"]:
        lines.append(f"proposal: {payload['proposal_explanation']}")
    if payload["validation_status"]:
        lines.append(f"validation: {payload['validation_status']} ({payload['validation_summary']})")
    return "\n".join(lines)


def _format_runs_payload(payload: list[dict[str, object]]) -> str:
    if not payload:
        return "no runs recorded"
    lines = []
    for run in payload[:10]:
        lines.append(f"- {run['run_id']} {run['workflow_type']} status={run['status']} tool={run['tool']['name']}")
    return "\n".join(lines)


def _format_run_show(payload: dict[str, object]) -> str:
    return "\n".join(
        [
            f"run: {payload['run_id']}",
            f"workflow: {payload['workflow_type']}",
            f"status: {payload['status']}",
            f"tool: {payload['tool']['name']}",
            f"summary: {payload['summary']}",
        ]
    )


def _format_replay_payload(payload: dict[str, object]) -> str:
    return f"replay exit_code={payload['exit_code']}\nstdout:\n{payload['stdout']}\nstderr:\n{payload['stderr']}"


def _format_eval_payload(payload: dict[str, object]) -> str:
    return f"suite={payload['suite']} passed={payload['passed']}/{payload['total']}"
