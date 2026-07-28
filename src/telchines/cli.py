from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer

from telchines.errors import AdapterExecutionError, ConfigError, ProviderError, WorkflowInputError
from telchines.onboarding import initialize_and_index_get_started, inspect_get_started
from telchines.presentation import (
    render_adapters_payload,
    render_doctor_summary,
    render_get_started,
    render_index_status_payload,
    render_project_init,
    render_project_templates,
    render_provider_payload,
    render_payload,
    render_retrieval_payload,
    render_recipe_result,
    render_run_show,
    render_runs_payload,
)
from telchines import __version__
from telchines.operations import (
    agent as agent_op,
    check_adapters as check_adapters_op,
    check_providers as check_providers_op,
    clean_index as clean_index_op,
    coverage_import as coverage_import_op,
    coverage_plan as coverage_plan_op,
    doctor_summary as doctor_summary_op,
    doctor_runs as doctor_runs_op,
    dump_json,
    format_coverage_human,
    format_triage_ci,
    format_triage_human,
    gen_cocotb as gen_cocotb_op,
    gen_sva as gen_sva_op,
    import_runs as import_runs_op,
    import_runs_from_ci as import_runs_from_ci_op,
    index_status as index_status_op,
    index_project as index_project_op,
    inspect_waveform as inspect_waveform_op,
    initialize_project,
    list_adapters as list_adapters_op,
    list_model_options as list_model_options_op,
    list_providers as list_providers_op,
    list_runs as list_runs_op,
    list_waveforms as list_waveforms_op,
    load_eval_report,
    privacy_report as privacy_report_op,
    project_templates as project_templates_op,
    purge_artifacts as purge_artifacts_op,
    repair as repair_op,
    replay_run as replay_run_op,
    retrieve_query,
    review_artifact as review_artifact_op,
    run_eval as run_eval_op,
    select_model_provider as select_model_provider_op,
    setup_provider as setup_provider_op,
    set_provider_model as set_provider_model_op,
    set_provider_reasoning as set_provider_reasoning_op,
    show_run as show_run_op,
    show_waveform as show_waveform_op,
    triage as triage_op,
    waveform_signals as waveform_signals_op,
)
from telchines.shell import run_shell
from telchines.setup import clear_shell_history, run_setup, set_shell_history_enabled, shell_history_status

app = typer.Typer(
    help=(
        "Telchines CLI — grounded verification workflows for hardware teams.\n\n"
        "Common paths: investigate regressions with `tel triage`; generate assertions with `tel gen-sva`; "
        "plan coverage closure with `tel coverage-plan`; inspect context with `tel retrieve`."
    ),
    invoke_without_command=True,
    add_completion=False,
)
project_app = typer.Typer(help="Initialize and inspect Telchines project state.", no_args_is_help=True)
index_app = typer.Typer(help="Build and inspect the retrieval index.", invoke_without_command=True)
runs_app = typer.Typer(help="Inspect, import, and replay stored verification runs.", no_args_is_help=True)
coverage_app = typer.Typer(help="Normalize coverage exports for closure planning.", no_args_is_help=True)
eval_app = typer.Typer(help="Run or inspect the offline evaluation suite.", no_args_is_help=True)
adapters_app = typer.Typer(help="List and check supported verification tools.", no_args_is_help=True)
providers_app = typer.Typer(help="Configure and inspect model providers.", no_args_is_help=True)
waveforms_app = typer.Typer(help="Inspect stored or source waveform data.", no_args_is_help=True)
artifacts_app = typer.Typer(help="Review or safely purge generated artifacts.", no_args_is_help=True)
doctor_app = typer.Typer(help="Inspect privacy and project diagnostics.", invoke_without_command=True)
history_app = typer.Typer(help="Control private, opt-in shell command history.", no_args_is_help=True)
app.add_typer(project_app, name="project")
app.add_typer(index_app, name="index")
app.add_typer(runs_app, name="runs")
app.add_typer(coverage_app, name="coverage")
app.add_typer(eval_app, name="eval")
app.add_typer(adapters_app, name="adapters")
app.add_typer(providers_app, name="providers")
app.add_typer(waveforms_app, name="waveforms")
app.add_typer(artifacts_app, name="artifacts")
app.add_typer(doctor_app, name="doctor")
app.add_typer(history_app, name="history")


def _fail(message: str, exit_code: int = 2) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code=exit_code)


def _emit_format(payload: object, output_format: str, human_renderer) -> None:  # noqa: ANN001
    if output_format == "json":
        typer.echo(dump_json(payload))
        return
    if output_format == "human":
        _echo_human(human_renderer(payload))
        return
    _fail("--format must be json or human")


def _echo_human(value: str) -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    typer.echo(value.encode(encoding, errors="replace").decode(encoding, errors="replace"))


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
    """Open the interactive shell with command completion and readable results."""
    if plain and fullscreen:
        _fail("--plain and --fullscreen cannot be used together")
    mode = "plain" if plain else "fullscreen" if fullscreen else "auto"
    run_shell(Path.cwd(), mode=mode)


@app.command("setup")
def setup_command() -> None:
    """Configure user-level provider and privacy defaults without creating a project."""
    try:
        typer.echo(run_setup())
    except ConfigError as exc:
        _fail(f"setup error: {exc}")


@app.command("get-started")
def get_started(
    initialize: bool = typer.Option(False, "--init", help="Confirm initialization and build the first retrieval index."),
    yes: bool = typer.Option(False, "--yes", help="Confirm --init without an interactive prompt."),
) -> None:
    """Inspect this directory and recommend the first useful Telchines workflow."""
    root = Path.cwd()
    if not initialize:
        _echo_human(render_get_started(inspect_get_started(root)))
        return
    if not yes and not typer.confirm(f"Initialize Telchines in {root} and build its index?", default=False):
        _echo_human(render_get_started(inspect_get_started(root)))
        typer.echo("No changes were made. Run `tel get-started --init` when you are ready.")
        return
    try:
        _echo_human(render_get_started(initialize_and_index_get_started(root)))
    except ConfigError as exc:
        _fail(f"config error: {exc}")


@project_app.command("init")
def project_init(
    path: Path = typer.Argument(Path(".")),
    name: Optional[str] = typer.Option(None, "--name"),
    template: Optional[str] = typer.Option(None, "--template", help="Built-in scaffold template to apply."),
) -> None:
    try:
        config = initialize_project(path, name=name, template=template)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    _echo_human(render_project_init(config.project.root_path, config.project.project_id, template))


@project_app.command("templates")
def project_templates(output_format: str = typer.Option("json", "--format", help="Output format: json or human.")) -> None:
    try:
        payload = project_templates_op()
    except ValueError as exc:
        _fail(str(exc))
    _emit_format(payload, output_format, render_project_templates)


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
def index_status(output_format: str = typer.Option("json", "--format", help="Output format: json or human.")) -> None:
    try:
        payload = index_status_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    _emit_format(payload, output_format, render_index_status_payload)


@index_app.command("clean")
def index_clean() -> None:
    try:
        payload = clean_index_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@app.command("retrieve")
def retrieve(
    query: str,
    limit: int = typer.Option(5, "--limit"),
    mode: str = typer.Option("general", "--mode"),
    output_format: str = typer.Option("json", "--format", help="Output format: json or human."),
) -> None:
    """Search indexed project context and return grounded citations."""
    try:
        payload = retrieve_query(None, query, limit=limit, mode=mode)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    _emit_format(payload, output_format, render_retrieval_payload)


@runs_app.command("list")
def list_runs(output_format: str = typer.Option("json", "--format", help="Output format: json or human.")) -> None:
    try:
        payload = list_runs_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    _emit_format(payload, output_format, render_runs_payload)


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
def show_run(run_id: str, output_format: str = typer.Option("json", "--format", help="Output format: json or human.")) -> None:
    try:
        payload = show_run_op(None, run_id)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    _emit_format(payload, output_format, render_run_show)


@runs_app.command("replay")
def replay_run(
    run_id: str,
    yes: bool = typer.Option(False, "--yes", help="Execute the stored replay command. Without this, only show what would run."),
    output_format: str = typer.Option("json", "--format", help="Output format: json or human."),
) -> None:
    try:
        payload = replay_run_op(None, run_id, confirm=yes)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    _emit_format(payload, output_format, lambda value: render_payload("Replay", value))
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
    output_format: str = typer.Option("json", "--format", help="Output format: json or human."),
) -> None:
    """Propose and validate a minimal adapter-backed repair."""
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
    """Plan and run a review-gated hardware verification task."""
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
    """Cluster regression failures and suggest the next investigation step."""
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
    """Classify coverage gaps and rank closure actions."""
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
    _emit_format(payload, output_format, lambda value: render_payload("Repair Result", value))


@app.command("diagnose-regressions")
def diagnose_regressions(
    log_path: Path = typer.Argument(..., help="Regression log file or directory."),
    waveforms: list[Path] = typer.Option([], "--waveform", help="Related waveform path; repeat as needed."),
    output_format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """Triage regression failures with the common inputs exposed directly."""
    try:
        payload = triage_op(None, [log_path], waveforms=waveforms or None)
    except (ConfigError, WorkflowInputError, ValueError) as exc:
        _fail(f"input error: {exc}")
    if output_format == "json":
        typer.echo(dump_json(payload))
    elif output_format == "human":
        _echo_human(render_recipe_result("Regression Diagnosis", payload, f"tel runs show {payload['run_id']}"))
    else:
        _fail("--format must be json or human")


@app.command("fix-compile")
def fix_compile(
    file: str = typer.Argument(..., help="Source file to repair."),
    tool: str = typer.Option(..., "--tool", help="Verification adapter to run."),
    apply_patch: bool = typer.Option(False, "--apply", help="Apply a validated repair patch."),
    output_format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """Propose a review-gated compile repair with a minimal command surface."""
    try:
        payload = repair_op(None, tool=tool, files=[file], apply_patch=apply_patch)
    except KeyError:
        _fail(f"unknown adapter: {tool}")
    except (ConfigError, WorkflowInputError, AdapterExecutionError, ProviderError, ValueError) as exc:
        _fail(f"repair error: {exc}")
    if output_format == "json":
        typer.echo(dump_json(payload))
    elif output_format == "human":
        _echo_human(render_recipe_result("Compile Repair", payload, f"tel artifacts review {payload.get('patch_id') or payload['run_id']}"))
    else:
        _fail("--format must be json or human")


@app.command("draft-assertions")
def draft_assertions(
    spec: Path = typer.Option(..., "--spec", help="Specification or design context file."),
    rtl: Path = typer.Option(..., "--rtl", help="RTL file to target."),
    output_format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """Generate a first assertion draft from a specification and RTL file."""
    try:
        payload = gen_sva_op(None, spec=spec, rtl=rtl)
    except (ConfigError, WorkflowInputError, ProviderError) as exc:
        _fail(f"generation error: {exc}")
    if output_format == "json":
        typer.echo(dump_json(payload))
    elif output_format == "human":
        _echo_human(render_recipe_result("Assertion Draft", payload, f"tel artifacts review {payload.get('candidate_id') or payload.get('artifact_path')}"))
    else:
        _fail("--format must be json or human")


@app.command("scaffold-cocotb")
def scaffold_cocotb(
    dut: Path = typer.Option(..., "--dut", help="DUT RTL file."),
    spec: Path | None = typer.Option(None, "--spec", help="Optional specification or design context file."),
    output_format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """Generate a grounded cocotb starter testbench with common inputs only."""
    try:
        payload = gen_cocotb_op(None, dut=dut, spec=spec)
    except (ConfigError, WorkflowInputError, ProviderError) as exc:
        _fail(f"generation error: {exc}")
    if output_format == "json":
        typer.echo(dump_json(payload))
    elif output_format == "human":
        _echo_human(render_recipe_result("Cocotb Scaffold", payload, f"tel artifacts review {payload.get('candidate_id') or payload.get('artifact_path')}"))
    else:
        _fail("--format must be json or human")


@coverage_app.command("import")
def coverage_import(
    source: Path = typer.Argument(..., help="Coverage export to normalize."),
    source_format: str = typer.Option(..., "--format", help="Source format: telchines-json, ucis-json, vivado, quartus, or questa-text."),
    output: Path = typer.Option(..., "--output", help="Normalized Telchines coverage JSON output path."),
) -> None:
    try:
        payload = coverage_import_op(None, source, source_format=source_format, output=output)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except WorkflowInputError as exc:
        _fail(f"input error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
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
    output_format: str = typer.Option("json", "--format", help="Output format: json or human."),
) -> None:
    """Generate and validate first-pass assertions from specification and RTL."""
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
    _emit_format(payload, output_format, lambda value: render_payload("Spec-to-SVA Result", value))


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
    output_format: str = typer.Option("json", "--format", help="Output format: json or human."),
) -> None:
    """Generate a grounded cocotb starter testbench for a DUT."""
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
    _emit_format(payload, output_format, lambda value: render_payload("DUT-to-Cocotb Result", value))


@providers_app.command("list")
def providers_list(output_format: str = typer.Option("json", "--format", help="Output format: json or human.")) -> None:
    try:
        payload = list_providers_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    _emit_format(payload, output_format, render_provider_payload)


@providers_app.command("check")
def providers_check(
    name: Optional[str] = typer.Argument(None),
    offline: bool = typer.Option(False, "--offline"),
    output_format: str = typer.Option("json", "--format", help="Output format: json or human."),
) -> None:
    try:
        payload = check_providers_op(None, provider_name=name, live=not offline)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    _emit_format(payload, output_format, lambda value: render_payload("Provider Checks", value))
    if payload["status"] != "passed":
        raise typer.Exit(code=1)


@providers_app.command("models")
def providers_models(name: Optional[str] = typer.Argument(None), offline: bool = typer.Option(False, "--offline")) -> None:
    try:
        payload = list_model_options_op(None, live=not offline)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    if name is not None:
        providers = [provider for provider in payload.get("providers", []) if isinstance(provider, dict) and provider.get("name") == name]
        if not providers:
            _fail(f"config error: provider {name} is not configured")
        payload = {**payload, "providers": providers}
    typer.echo(dump_json(payload))


@runs_app.command("import-junit")
def import_junit(
    source: Path = typer.Argument(..., help="JUnit XML report to normalize and import."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and preview imported runs without storing them."),
) -> None:
    _import_ci("junit", source, dry_run=dry_run)


@runs_app.command("import-github-actions")
def import_github_actions(
    source: Path = typer.Argument(..., help="GitHub Actions JSON export to normalize and import."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and preview imported runs without storing them."),
) -> None:
    _import_ci("github-actions", source, dry_run=dry_run)


@runs_app.command("import-jenkins")
def import_jenkins(
    source: Path = typer.Argument(..., help="Jenkins JSON export to normalize and import."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and preview imported runs without storing them."),
) -> None:
    _import_ci("jenkins", source, dry_run=dry_run)


def _import_ci(importer: str, source: Path, *, dry_run: bool) -> None:
    try:
        payload = import_runs_from_ci_op(None, source, importer=importer, dry_run=dry_run)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    typer.echo(dump_json(payload))


@providers_app.command("select")
def providers_select(
    capability: str = typer.Option(..., "--capability", help="Capability to route: repair or generation."),
    provider: str = typer.Option(..., "--provider", help="Provider to use as the default for the capability."),
) -> None:
    try:
        payload = select_model_provider_op(None, capability, provider)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@providers_app.command("set-model")
def providers_set_model(name: str = typer.Argument(...), model: str = typer.Argument(...)) -> None:
    try:
        payload = set_provider_model_op(None, name, model)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@providers_app.command("set-reasoning")
def providers_set_reasoning(name: str = typer.Argument(...), level: str = typer.Argument(...)) -> None:
    try:
        payload = set_provider_reasoning_op(None, name, level)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@providers_app.command("setup")
def providers_setup(
    name: str = typer.Argument(...),
    kind: str = typer.Option(..., "--kind", help="Provider setup kind: openai-compatible, anthropic, or local-openai."),
    capability: Optional[list[str]] = typer.Option(None, "--capability", help="Capability to enable; repeat for repair and generation."),
    model: Optional[str] = typer.Option(None, "--model", help="Model identifier to store in config."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Provider base URL."),
    api_key_env: Optional[str] = typer.Option(None, "--api-key-env", help="Environment variable that holds the API key."),
    auth: Optional[str] = typer.Option(None, "--auth", help="Auth mode for OpenAI-compatible providers: bearer or none."),
    timeout_seconds: Optional[int] = typer.Option(None, "--timeout-seconds", help="Provider timeout in seconds."),
    select_defaults: bool = typer.Option(False, "--select-defaults", help="Make this provider the default for its capabilities."),
) -> None:
    try:
        payload = setup_provider_op(
            None,
            name,
            kind=kind,
            capabilities=capability,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            auth=auth,
            timeout_seconds=timeout_seconds,
            select_defaults=select_defaults,
        )
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@artifacts_app.command("purge")
def artifacts_purge(
    yes: bool = typer.Option(False, "--yes", help="Actually delete artifact files. Without this, only report what would be removed."),
    scope: Optional[list[str]] = typer.Option(None, "--scope", help="Artifact scope to purge; repeat for task-artifacts, reports, waveforms, patches, generations, or generated."),
    older_than_days: Optional[int] = typer.Option(None, "--older-than-days", help="Only purge artifact files at least this many days old."),
    output_format: str = typer.Option("json", "--format", help="Output format: json or human."),
) -> None:
    try:
        payload = purge_artifacts_op(None, dry_run=not yes, scopes=scope, older_than_days=older_than_days)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(f"input error: {exc}")
    _emit_format(payload, output_format, lambda value: render_payload("Artifact Purge", value))


@artifacts_app.command("review")
def artifacts_review(
    reference: str = typer.Argument(..., help="Generation candidate id, validation run id, or generated artifact path."),
    max_diff_lines: int = typer.Option(200, "--max-diff-lines", help="Maximum unified diff lines to include in JSON output."),
    output_format: str = typer.Option("json", "--format", help="Output format: json or human."),
) -> None:
    try:
        payload = review_artifact_op(None, reference=reference, max_diff_lines=max_diff_lines)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    _emit_format(payload, output_format, lambda value: render_payload("Artifact Review", value))


@doctor_app.command("privacy")
def doctor_privacy() -> None:
    try:
        payload = privacy_report_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(dump_json(payload))


@doctor_app.callback()
def doctor(ctx: typer.Context) -> None:
    """Show project health when no doctor subcommand is selected."""
    if ctx.invoked_subcommand is not None:
        return
    _echo_human(render_doctor_summary(doctor_summary_op()))


@history_app.command("status")
def history_status() -> None:
    typer.echo(dump_json(shell_history_status()))


@history_app.command("enable")
def history_enable() -> None:
    typer.echo(dump_json(set_shell_history_enabled(True)))


@history_app.command("disable")
def history_disable() -> None:
    typer.echo(dump_json(set_shell_history_enabled(False)))


@history_app.command("clear")
def history_clear(yes: bool = typer.Option(False, "--yes", help="Delete saved private shell history.")) -> None:
    if not yes and not typer.confirm("Delete saved Telchines shell history?", default=False):
        typer.echo("History was not deleted.")
        return
    clear_shell_history()
    typer.echo("Saved shell history cleared.")


@adapters_app.command("list")
def adapters_list(
    category: str | None = typer.Option(None, "--category"),
    output_format: str = typer.Option("json", "--format", help="Output format: json or human."),
) -> None:
    try:
        payload = list_adapters_op(category=category)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    _emit_format(payload, output_format, render_adapters_payload)


@adapters_app.command("check")
def adapters_check(
    name: Optional[str] = typer.Argument(None),
    category: str | None = typer.Option(None, "--category"),
    output_format: str = typer.Option("json", "--format", help="Output format: json or human."),
) -> None:
    try:
        payload = check_adapters_op(None, adapter_name=name, category=category)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except KeyError as exc:
        _fail(f"unknown adapter: {exc.args[0]}")
    _emit_format(payload, output_format, lambda value: render_payload("Adapter Checks", value))
    if any(item["status"] != "passed" for item in payload["adapters"]):
        raise typer.Exit(code=1)


@waveforms_app.command("list")
def waveforms_list(output_format: str = typer.Option("json", "--format", help="Output format: json or human.")) -> None:
    try:
        payload = list_waveforms_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    _emit_format(payload, output_format, lambda value: render_payload("Waveforms", value))


@waveforms_app.command("show")
def waveforms_show(target: str = typer.Argument(...), output_format: str = typer.Option("json", "--format", help="Output format: json or human.")) -> None:
    try:
        payload = show_waveform_op(None, target)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    _emit_format(payload, output_format, lambda value: render_payload("Waveform Summary", value))


@waveforms_app.command("signals")
def waveforms_signals(target: str = typer.Argument(...), signal_filter: str | None = typer.Option(None, "--filter"), output_format: str = typer.Option("json", "--format", help="Output format: json or human.")) -> None:
    try:
        payload = waveform_signals_op(None, target, signal_filter=signal_filter)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    _emit_format(payload, output_format, lambda value: render_payload("Waveform Signals", value))


@waveforms_app.command("inspect")
def waveforms_inspect(
    target: str = typer.Argument(...),
    signal: str = typer.Option(..., "--signal"),
    window: int = typer.Option(8, "--window"),
    start_time: int | None = typer.Option(None, "--start-time", help="First VCD timestamp to include."),
    end_time: int | None = typer.Option(None, "--end-time", help="Last VCD timestamp to include."),
    log_path: str | None = typer.Option(None, "--log", help="Project-relative simulator log to correlate by time."),
    tolerance_ticks: int = typer.Option(0, "--tolerance-ticks", help="Timestamp tolerance in VCD ticks for log correlation."),
    output_format: str = typer.Option("json", "--format", help="Output format: json or human."),
) -> None:
    try:
        payload = inspect_waveform_op(
            None,
            target,
            signal=signal,
            window=window,
            start_time=start_time,
            end_time=end_time,
            log_path=log_path,
            tolerance_ticks=tolerance_ticks,
        )
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    except ValueError as exc:
        _fail(str(exc))
    _emit_format(payload, output_format, lambda value: render_payload("Waveform Inspection", value))


@eval_app.command("run")
def eval_run(output_format: str = typer.Option("json", "--format", help="Output format: json or human.")) -> None:
    try:
        payload = run_eval_op()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    _emit_format(payload, output_format, lambda value: render_payload("Evaluation", value))


@eval_app.command("report")
def eval_report(output_format: str = typer.Option("json", "--format", help="Output format: json or human.")) -> None:
    try:
        payload = load_eval_report()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    _emit_format(payload, output_format, lambda value: render_payload("Evaluation Report", value))
