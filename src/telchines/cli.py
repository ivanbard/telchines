from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from telchines.errors import AdapterExecutionError, ConfigError, ProviderError
from telchines.operations import (
    dump_json,
    format_triage_ci,
    format_triage_human,
    gen_cocotb as gen_cocotb_op,
    gen_sva as gen_sva_op,
    index_project as index_project_op,
    inspect_waveform as inspect_waveform_op,
    initialize_project,
    list_adapters as list_adapters_op,
    list_providers as list_providers_op,
    list_runs as list_runs_op,
    list_waveforms as list_waveforms_op,
    load_eval_report,
    repair as repair_op,
    replay_run as replay_run_op,
    retrieve_query,
    run_eval as run_eval_op,
    show_run as show_run_op,
    show_waveform as show_waveform_op,
    triage as triage_op,
    waveform_signals as waveform_signals_op,
)
from telchines.shell import run_shell

app = typer.Typer(help="Telchines CLI", invoke_without_command=True, add_completion=False)
project_app = typer.Typer(no_args_is_help=True)
runs_app = typer.Typer(no_args_is_help=True)
eval_app = typer.Typer(no_args_is_help=True)
adapters_app = typer.Typer(no_args_is_help=True)
providers_app = typer.Typer(no_args_is_help=True)
waveforms_app = typer.Typer(no_args_is_help=True)
app.add_typer(project_app, name="project")
app.add_typer(runs_app, name="runs")
app.add_typer(eval_app, name="eval")
app.add_typer(adapters_app, name="adapters")
app.add_typer(providers_app, name="providers")
app.add_typer(waveforms_app, name="waveforms")


def _fail(message: str, exit_code: int = 2) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code=exit_code)


@app.callback()
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        run_shell(Path.cwd())
        raise typer.Exit()


@app.command("shell")
def shell_command() -> None:
    run_shell(Path.cwd())


@project_app.command("init")
def project_init(path: Path = typer.Argument(Path(".")), name: Optional[str] = typer.Option(None, "--name")) -> None:
    try:
        config = initialize_project(path, name=name)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(f"initialized project {config.project.project_id} at {config.project.root_path}")


@app.command("index")
def index_project() -> None:
    try:
        chunk_count = index_project_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(f"indexed {chunk_count} chunks")


@app.command("retrieve")
def retrieve(query: str, limit: int = typer.Option(5, "--limit"), mode: str = typer.Option("general", "--mode")) -> None:
    try:
        payload = retrieve_query(None, query, limit=limit, mode=mode)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@runs_app.command("list")
def list_runs() -> None:
    try:
        payload = list_runs_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@runs_app.command("show")
def show_run(run_id: str) -> None:
    try:
        payload = show_run_op(None, run_id)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@runs_app.command("replay")
def replay_run(run_id: str) -> None:
    try:
        payload = replay_run_op(None, run_id)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    typer.echo(dump_json(payload))


@app.command("repair")
def repair(
    tool: str = typer.Option(..., "--tool"),
    files: list[str] = typer.Option(..., "--file"),
    extra_arg: list[str] = typer.Option([], "--extra-arg"),
    apply_patch: bool = typer.Option(False, "--apply"),
) -> None:
    try:
        payload = repair_op(None, tool=tool, files=files, extra_arg=extra_arg, apply_patch=apply_patch)
    except KeyError:
        _fail(f"unknown adapter: {tool}")
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except AdapterExecutionError as exc:
        _fail(f"adapter error: {exc}")
    except ProviderError as exc:
        _fail(f"provider error: {exc}")
    typer.echo(dump_json(payload))


@app.command("triage")
def triage(
    logs: list[Path] = typer.Option(..., "--logs"),
    waveforms: list[Path] = typer.Option([], "--waveform"),
    output_format: str = typer.Option("json", "--format"),
) -> None:
    try:
        payload = triage_op(None, logs, waveforms=waveforms or None)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    if output_format == "human":
        typer.echo(format_triage_human(payload))
        return
    if output_format == "ci":
        typer.echo(dump_json(format_triage_ci(payload)))
        return
    typer.echo(dump_json(payload))


@app.command("gen-sva")
def gen_sva(
    spec: Path = typer.Option(..., "--spec"),
    rtl: Path = typer.Option(..., "--rtl"),
    output: Path | None = typer.Option(None, "--output"),
    provider: str | None = typer.Option(None, "--provider"),
) -> None:
    try:
        payload = gen_sva_op(None, spec=spec, rtl=rtl, output=output, provider_name=provider)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ProviderError as exc:
        _fail(f"provider error: {exc}")
    typer.echo(dump_json(payload))


@app.command("gen-cocotb")
def gen_cocotb(
    dut: Path = typer.Option(..., "--dut"),
    spec: Path | None = typer.Option(None, "--spec"),
    output_dir: Path | None = typer.Option(None, "--output-dir"),
    intent: str = typer.Option("", "--intent"),
    provider: str | None = typer.Option(None, "--provider"),
) -> None:
    try:
        payload = gen_cocotb_op(None, dut=dut, spec=spec, output_dir=output_dir, intent=intent, provider_name=provider)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ProviderError as exc:
        _fail(f"provider error: {exc}")
    typer.echo(dump_json(payload))


@providers_app.command("list")
def providers_list() -> None:
    try:
        payload = list_providers_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@adapters_app.command("list")
def adapters_list(category: str | None = typer.Option(None, "--category")) -> None:
    try:
        payload = list_adapters_op(category=category)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@waveforms_app.command("list")
def waveforms_list() -> None:
    try:
        payload = list_waveforms_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@waveforms_app.command("show")
def waveforms_show(target: str = typer.Argument(...)) -> None:
    try:
        payload = show_waveform_op(None, target)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    typer.echo(dump_json(payload))


@waveforms_app.command("signals")
def waveforms_signals(target: str = typer.Argument(...), signal_filter: str | None = typer.Option(None, "--filter")) -> None:
    try:
        payload = waveform_signals_op(None, target, signal_filter=signal_filter)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    typer.echo(dump_json(payload))


@waveforms_app.command("inspect")
def waveforms_inspect(
    target: str = typer.Argument(...),
    signal: str = typer.Option(..., "--signal"),
    window: int = typer.Option(8, "--window"),
) -> None:
    try:
        payload = inspect_waveform_op(None, target, signal=signal, window=window)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    typer.echo(dump_json(payload))


@eval_app.command("run")
def eval_run() -> None:
    try:
        payload = run_eval_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@eval_app.command("report")
def eval_report() -> None:
    try:
        payload = load_eval_report()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))
