from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from telchines.errors import AdapterExecutionError, ConfigError, ProviderError, WorkflowInputError
from telchines import __version__
from telchines.operations import (
    agent as agent_op,
    check_adapters as check_adapters_op,
    check_providers as check_providers_op,
    clean_index as clean_index_op,
    coverage_plan as coverage_plan_op,
    doctor_runs as doctor_runs_op,
    dump_json,
    format_coverage_human,
    format_triage_ci,
    format_triage_human,
    gen_cocotb as gen_cocotb_op,
    gen_sva as gen_sva_op,
    import_runs as import_runs_op,
    index_status as index_status_op,
    index_project as index_project_op,
    inspect_waveform as inspect_waveform_op,
    initialize_project,
    list_adapters as list_adapters_op,
    list_providers as list_providers_op,
    list_runs as list_runs_op,
    list_waveforms as list_waveforms_op,
    load_eval_report,
    privacy_report as privacy_report_op,
    purge_artifacts as purge_artifacts_op,
    repair as repair_op,
    replay_run as replay_run_op,
    retrieve_query,
    review_artifact as review_artifact_op,
    run_eval as run_eval_op,
    show_run as show_run_op,
    show_waveform as show_waveform_op,
    triage as triage_op,
    waveform_signals as waveform_signals_op,
)
from telchines.shell import run_shell

app = typer.Typer(help="Telchines CLI", invoke_without_command=True, add_completion=False)
project_app = typer.Typer(no_args_is_help=True)
index_app = typer.Typer(invoke_without_command=True)
runs_app = typer.Typer(no_args_is_help=True)
eval_app = typer.Typer(no_args_is_help=True)
adapters_app = typer.Typer(no_args_is_help=True)
providers_app = typer.Typer(no_args_is_help=True)
waveforms_app = typer.Typer(no_args_is_help=True)
artifacts_app = typer.Typer(no_args_is_help=True)
doctor_app = typer.Typer(no_args_is_help=True)
app.add_typer(project_app, name="project")
app.add_typer(index_app, name="index")
app.add_typer(runs_app, name="runs")
app.add_typer(eval_app, name="eval")
app.add_typer(adapters_app, name="adapters")
app.add_typer(providers_app, name="providers")
app.add_typer(waveforms_app, name="waveforms")
app.add_typer(artifacts_app, name="artifacts")
app.add_typer(doctor_app, name="doctor")


def _fail(message: str, exit_code: int = 2) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code=exit_code)


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(f"telchines {__version__}")
    raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the installed Telchines version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    if ctx.invoked_subcommand is None:
        run_shell(Path.cwd())
        raise typer.Exit()


@app.command("shell")
def shell_command(
    plain: bool = typer.Option(False, "--plain", help="Run the plain stdin/stdout shell."),
    fullscreen: bool = typer.Option(False, "--fullscreen", help="Run the prompt_toolkit full-screen shell."),
) -> None:
    if plain and fullscreen:
        _fail("--plain and --fullscreen cannot be used together")
    mode = "plain" if plain else "fullscreen" if fullscreen else "auto"
    run_shell(Path.cwd(), mode=mode)


@project_app.command("init")
def project_init(path: Path = typer.Argument(Path(".")), name: Optional[str] = typer.Option(None, "--name")) -> None:
    try:
        config = initialize_project(path, name=name)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(f"initialized project {config.project.project_id} at {config.project.root_path}")


@index_app.callback()
def index_project(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    try:
        chunk_count = index_project_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(f"indexed {chunk_count} chunks")
    raise typer.Exit()


@index_app.command("status")
def index_status() -> None:
    try:
        payload = index_status_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@index_app.command("clean")
def index_clean() -> None:
    try:
        payload = clean_index_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


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


@runs_app.command("doctor")
def doctor_runs() -> None:
    try:
        payload = doctor_runs_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))
    if payload.get("status") == "warning":
        raise typer.Exit(code=1)


@runs_app.command("show")
def show_run(run_id: str) -> None:
    try:
        payload = show_run_op(None, run_id)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@runs_app.command("replay")
def replay_run(
    run_id: str,
    yes: bool = typer.Option(False, "--yes", help="Execute the stored replay command. Without this, only show what would run."),
) -> None:
    try:
        payload = replay_run_op(None, run_id, confirm=yes)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    typer.echo(dump_json(payload))
    if payload.get("status") == "confirmation_required":
        raise typer.Exit(code=1)


@runs_app.command("import")
def import_runs(
    manifest: Path = typer.Argument(..., help="Regression manager or test-runner import manifest."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and preview imported runs without storing them."),
) -> None:
    try:
        payload = import_runs_op(None, manifest, dry_run=dry_run)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    typer.echo(dump_json(payload))


@app.command("repair")
def repair(
    tool: str = typer.Option(..., "--tool"),
    files: list[str] = typer.Option([], "--file"),
    extra_arg: list[str] = typer.Option([], "--extra-arg"),
    adapter_arg: list[str] = typer.Option([], "--adapter-arg"),
    filelists: list[str] = typer.Option([], "--filelist"),
    include_dirs: list[str] = typer.Option([], "--include-dir"),
    defines: list[str] = typer.Option([], "--define"),
    top_module: str | None = typer.Option(None, "--top"),
    work_library: str | None = typer.Option(None, "--worklib"),
    apply_patch: bool = typer.Option(False, "--apply"),
) -> None:
    try:
        payload = repair_op(
            None,
            tool=tool,
            files=files,
            extra_arg=extra_arg,
            apply_patch=apply_patch,
            adapter_args=adapter_arg,
            filelists=filelists,
            include_dirs=include_dirs,
            defines=defines,
            top_module=top_module,
            work_library=work_library,
        )
    except KeyError:
        _fail(f"unknown adapter: {tool}")
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except AdapterExecutionError as exc:
        _fail(f"adapter error: {exc}")
    except WorkflowInputError as exc:
        _fail(f"input error: {exc}")
    except AdapterExecutionError as exc:
        _fail(f"adapter error: {exc}")
    except ProviderError as exc:
        _fail(f"provider error: {exc}")
    typer.echo(dump_json(payload))


@app.command("agent")
def agent_command(
    task: str = typer.Argument(..., help="Natural-language hardware engineering task."),
    tool: str | None = typer.Option(None, "--tool", help="Adapter to run for repair tasks."),
    files: list[str] = typer.Option([], "--file", help="RTL/source file for adapter-backed repair."),
    extra_arg: list[str] = typer.Option([], "--extra-arg", help="Extra argument forwarded to the adapter."),
    adapter_arg: list[str] = typer.Option([], "--adapter-arg", help="Extra argument forwarded to the adapter."),
    filelists: list[str] = typer.Option([], "--filelist", help="Tool filelist for adapter-backed execution."),
    include_dirs: list[str] = typer.Option([], "--include-dir", help="Include directory for adapter-backed execution."),
    defines: list[str] = typer.Option([], "--define", help="Preprocessor define for adapter-backed execution."),
    top_module: str | None = typer.Option(None, "--top", help="Top module for adapter-backed execution."),
    work_library: str | None = typer.Option(None, "--worklib", help="Work library for adapter-backed execution."),
    apply_patch: bool = typer.Option(False, "--apply", help="Apply a validated repair patch instead of leaving it review-gated."),
    logs: list[Path] = typer.Option([], "--logs", help="Log path for triage tasks."),
    waveforms: list[Path] = typer.Option([], "--waveform", help="Waveform path for triage/evidence tasks."),
    report: Path | None = typer.Option(None, "--report", help="Coverage report path."),
    exclusions: Path | None = typer.Option(None, "--exclusions", help="Coverage exclusions path."),
    formal_run: str | None = typer.Option(None, "--formal-run", help="Formal run id to use as supporting evidence."),
    rtl: list[Path] = typer.Option([], "--rtl", help="RTL file for generation or coverage tasks."),
    spec: list[Path] = typer.Option([], "--spec", help="Spec/context file for generation or coverage tasks."),
    dut: Path | None = typer.Option(None, "--dut", help="DUT path for cocotb generation tasks."),
    output: Path | None = typer.Option(None, "--output", help="Generated SVA output path."),
    output_dir: Path | None = typer.Option(None, "--output-dir", help="Generated cocotb output directory."),
    provider: str | None = typer.Option(None, "--provider", help="Generation provider override."),
    intent: str = typer.Option("", "--intent", help="Additional generation intent."),
) -> None:
    try:
        payload = agent_op(
            None,
            task,
            tool=tool,
            files=files,
            extra_arg=extra_arg,
            apply_patch=apply_patch,
            logs=logs,
            waveforms=waveforms,
            report=report,
            exclusions=exclusions,
            formal_run_id=formal_run,
            rtl=rtl,
            spec=spec,
            dut=dut,
            output=output,
            output_dir=output_dir,
            provider_name=provider,
            intent=intent,
            adapter_args=adapter_arg,
            filelists=filelists,
            include_dirs=include_dirs,
            defines=defines,
            top_module=top_module,
            work_library=work_library,
        )
    except KeyError as exc:
        _fail(f"unknown adapter: {exc.args[0]}")
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except AdapterExecutionError as exc:
        _fail(f"adapter error: {exc}")
    except WorkflowInputError as exc:
        _fail(f"input error: {exc}")
    except AdapterExecutionError as exc:
        _fail(f"adapter error: {exc}")
    except ProviderError as exc:
        _fail(f"provider error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
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
    except WorkflowInputError as exc:
        _fail(f"input error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    if output_format == "human":
        typer.echo(format_triage_human(payload))
        return
    if output_format == "ci":
        typer.echo(dump_json(format_triage_ci(payload)))
        return
    typer.echo(dump_json(payload))


@app.command("coverage-plan")
def coverage_plan(
    report: Path = typer.Option(..., "--report"),
    exclusions: Path | None = typer.Option(None, "--exclusions"),
    formal_run: str | None = typer.Option(None, "--formal-run"),
    rtl: list[Path] = typer.Option([], "--rtl"),
    spec: list[Path] = typer.Option([], "--spec"),
    output_format: str = typer.Option("json", "--format"),
) -> None:
    try:
        payload = coverage_plan_op(None, report=report, exclusions=exclusions, formal_run_id=formal_run, rtl=rtl, spec=spec)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except WorkflowInputError as exc:
        _fail(f"input error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    if output_format == "human":
        typer.echo(format_coverage_human(payload))
        return
    typer.echo(dump_json(payload))


@app.command("gen-sva")
def gen_sva(
    spec: Path = typer.Option(..., "--spec"),
    rtl: Path = typer.Option(..., "--rtl"),
    output: Path | None = typer.Option(None, "--output"),
    provider: str | None = typer.Option(None, "--provider"),
    adapter_arg: list[str] = typer.Option([], "--adapter-arg"),
    filelists: list[str] = typer.Option([], "--filelist"),
    include_dirs: list[str] = typer.Option([], "--include-dir"),
    defines: list[str] = typer.Option([], "--define"),
    top_module: str | None = typer.Option(None, "--top"),
    work_library: str | None = typer.Option(None, "--worklib"),
) -> None:
    try:
        payload = gen_sva_op(
            None,
            spec=spec,
            rtl=rtl,
            output=output,
            provider_name=provider,
            adapter_args=adapter_arg,
            filelists=filelists,
            include_dirs=include_dirs,
            defines=defines,
            top_module=top_module,
            work_library=work_library,
        )
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except WorkflowInputError as exc:
        _fail(f"input error: {exc}")
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
    adapter_arg: list[str] = typer.Option([], "--adapter-arg"),
    filelists: list[str] = typer.Option([], "--filelist"),
    include_dirs: list[str] = typer.Option([], "--include-dir"),
    defines: list[str] = typer.Option([], "--define"),
    top_module: str | None = typer.Option(None, "--top"),
    work_library: str | None = typer.Option(None, "--worklib"),
) -> None:
    try:
        payload = gen_cocotb_op(
            None,
            dut=dut,
            spec=spec,
            output_dir=output_dir,
            intent=intent,
            provider_name=provider,
            adapter_args=adapter_arg,
            filelists=filelists,
            include_dirs=include_dirs,
            defines=defines,
            top_module=top_module,
            work_library=work_library,
        )
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except WorkflowInputError as exc:
        _fail(f"input error: {exc}")
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


@providers_app.command("check")
def providers_check(name: Optional[str] = typer.Argument(None), offline: bool = typer.Option(False, "--offline")) -> None:
    try:
        payload = check_providers_op(None, provider_name=name, live=not offline)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))
    if payload["status"] != "passed":
        raise typer.Exit(code=1)


@artifacts_app.command("purge")
def artifacts_purge(
    yes: bool = typer.Option(False, "--yes", help="Actually delete artifact files. Without this, only report what would be removed."),
) -> None:
    try:
        payload = purge_artifacts_op(None, dry_run=not yes)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@artifacts_app.command("review")
def artifacts_review(
    reference: str = typer.Argument(..., help="Generation candidate id, validation run id, or generated artifact path."),
    max_diff_lines: int = typer.Option(200, "--max-diff-lines", help="Maximum unified diff lines to include in JSON output."),
) -> None:
    try:
        payload = review_artifact_op(None, reference=reference, max_diff_lines=max_diff_lines)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    typer.echo(dump_json(payload))


@doctor_app.command("privacy")
def doctor_privacy() -> None:
    try:
        payload = privacy_report_op()
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


@adapters_app.command("check")
def adapters_check(name: Optional[str] = typer.Argument(None), category: str | None = typer.Option(None, "--category")) -> None:
    try:
        payload = check_adapters_op(None, adapter_name=name, category=category)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except KeyError as exc:
        _fail(f"unknown adapter: {exc.args[0]}")
    typer.echo(dump_json(payload))
    if any(item["status"] != "passed" for item in payload["adapters"]):
        raise typer.Exit(code=1)


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
