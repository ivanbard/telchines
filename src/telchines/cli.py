from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import typer

from telchines.adapters.registry import AdapterRegistry
from telchines.config import ProjectConfig
from telchines.errors import AdapterExecutionError, ConfigError, ProviderError
from telchines.eval import run_default_suite
from telchines.models import VerificationRun
from telchines.providers import build_repair_provider
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import dataclass_to_dict, stable_id, utc_now
from telchines.workflows.repair import execute_repair
from telchines.workflows.triage import triage_logs

app = typer.Typer(help="Telchines CLI", no_args_is_help=True, add_completion=False)
project_app = typer.Typer(no_args_is_help=True)
runs_app = typer.Typer(no_args_is_help=True)
eval_app = typer.Typer(no_args_is_help=True)
app.add_typer(project_app, name="project")
app.add_typer(runs_app, name="runs")
app.add_typer(eval_app, name="eval")


def _load_services(root: Path | None = None) -> tuple[ProjectConfig, RunStore, RetrievalService]:
    config = ProjectConfig.discover(root or Path.cwd())
    store = RunStore(config)
    retrieval = RetrievalService(config)
    return config, store, retrieval


def _fail(message: str, exit_code: int = 2) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code=exit_code)


@project_app.command("init")
def project_init(path: Path = typer.Argument(Path(".")), name: Optional[str] = typer.Option(None, "--name")) -> None:
    try:
        config = ProjectConfig.init_project(path, name=name)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(f"initialized project {config.project.project_id} at {config.project.root_path}")


@app.command("index")
def index_project() -> None:
    try:
        _, _, retrieval = _load_services()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    chunk_count = retrieval.build_index()
    typer.echo(f"indexed {chunk_count} chunks")


@app.command("retrieve")
def retrieve(query: str, limit: int = typer.Option(5, "--limit"), mode: str = typer.Option("general", "--mode")) -> None:
    try:
        _, store, retrieval = _load_services()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    context = retrieval.search(query, limit=limit, mode=mode)
    store.save_context(context)
    typer.echo(json.dumps({"context_id": context.context_id, "mode": context.mode, "hits": [asdict(hit) for hit in context.hits]}, indent=2))


@runs_app.command("list")
def list_runs() -> None:
    try:
        _, store, _ = _load_services()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(json.dumps([dataclass_to_dict(run) for run in store.list_runs()], indent=2))


@runs_app.command("show")
def show_run(run_id: str) -> None:
    try:
        _, store, _ = _load_services()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(json.dumps(dataclass_to_dict(store.load_run(run_id)), indent=2))


@runs_app.command("replay")
def replay_run(run_id: str) -> None:
    try:
        config, store, _ = _load_services()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    run = store.load_run(run_id)
    if not run.replay_command:
        _fail("run does not have a replay command")
    result = subprocess.run(run.replay_command, cwd=config.project_root, capture_output=True, text=True, check=False)
    typer.echo(json.dumps({"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}, indent=2))


@app.command("repair")
def repair(
    tool: str = typer.Option(..., "--tool"),
    files: list[str] = typer.Option(..., "--file"),
    extra_arg: list[str] = typer.Option([], "--extra-arg"),
    apply_patch: bool = typer.Option(False, "--apply"),
) -> None:
    try:
        config, store, retrieval = _load_services()
        registry = AdapterRegistry()
        adapter = registry.get(tool)
    except KeyError:
        _fail(f"unknown adapter: {tool}")
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    run_id = stable_id("run", config.project.project_id, tool, utc_now(), ",".join(files))
    try:
        execution = adapter.run(run_id, config.project_root, files, config.project_root / config.artifacts_dir, extra_args=extra_arg)
    except AdapterExecutionError as exc:
        _fail(f"adapter error: {exc}")
    store.save_observations(execution.observations)
    base_run = VerificationRun(
        run_id=run_id,
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="compile_repair",
        tool=adapter.tool_reference,
        inputs={"files": files, "project_root": str(config.project_root)},
        status="passed" if execution.exit_code == 0 else "failed",
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        exit_code=execution.exit_code,
        artifacts={"log_path": execution.log_path},
        observation_ids=[observation.observation_id for observation in execution.observations],
        summary=execution.summary,
        replay_command=execution.command,
    )
    store.save_run(base_run)
    try:
        provider = build_repair_provider(config)
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    try:
        proposal, validation_run, context = execute_repair(config, store, retrieval, provider, base_run, apply_patch=apply_patch)
    except ProviderError as exc:
        _fail(f"provider error: {exc}")
    payload = {
        "run_id": base_run.run_id,
        "status": base_run.status,
        "context_id": context.context_id,
        "patch_id": proposal.patch_id if proposal else None,
        "provider": proposal.provider if proposal else getattr(provider, "name", ""),
        "proposal_explanation": proposal.explanation if proposal else None,
        "evidence_paths": proposal.evidence_paths if proposal else [],
        "replay_artifacts": proposal.replay_artifacts if proposal else {},
        "validation_run_id": validation_run.run_id if validation_run else None,
        "validation_status": validation_run.status if validation_run else None,
        "validation_summary": validation_run.summary if validation_run else None,
    }
    typer.echo(json.dumps(payload, indent=2))


@app.command("triage")
def triage(logs: list[Path] = typer.Option(..., "--logs"), output_format: str = typer.Option("json", "--format")) -> None:
    try:
        config, store, retrieval = _load_services()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    run, clusters, context = triage_logs(config, store, retrieval, logs)
    payload = {
        "run_id": run.run_id,
        "cluster_count": len(clusters),
        "context_id": context.context_id,
        "clusters": [dataclass_to_dict(cluster) for cluster in clusters],
    }
    if output_format == "human":
        typer.echo(_format_triage_human(payload))
        return
    if output_format == "ci":
        typer.echo(json.dumps(_format_triage_ci(payload), indent=2))
        return
    typer.echo(json.dumps(payload, indent=2))


@eval_app.command("run")
def eval_run() -> None:
    try:
        config, store, _ = _load_services()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    report = run_default_suite(config, store)
    typer.echo(json.dumps(report, indent=2))


@eval_app.command("report")
def eval_report() -> None:
    try:
        _, store, _ = _load_services()
    except ConfigError as exc:
        _fail(f"config error: {exc}")
    typer.echo(json.dumps(store.load_report("latest_eval"), indent=2))


def _format_triage_ci(payload: dict[str, object]) -> dict[str, object]:
    clusters = payload["clusters"]
    return {
        "status": "needs_attention" if clusters else "clean",
        "run_id": payload["run_id"],
        "cluster_count": payload["cluster_count"],
        "clusters": [
            {
                "cluster_id": cluster["cluster_id"],
                "signature": cluster["signature"],
                "count": cluster["count"],
                "summary": cluster["summary"],
                "likely_cause": cluster["likely_cause"],
                "suggested_action": cluster["suggested_action"],
                "evidence": [hit["citation"] for hit in cluster["evidence_hits"]],
                "similar_runs": [match["run_id"] for match in cluster["similar_runs"]],
            }
            for cluster in clusters
        ],
    }


def _format_triage_human(payload: dict[str, object]) -> str:
    lines = [f"run {payload['run_id']} produced {payload['cluster_count']} cluster(s)"]
    for index, cluster in enumerate(payload["clusters"], start=1):
        evidence = ", ".join(hit["citation"] for hit in cluster["evidence_hits"][:3]) or "none"
        similar = ", ".join(match["run_id"] for match in cluster["similar_runs"]) or "none"
        lines.extend(
            [
                f"",
                f"{index}. {cluster['summary']}",
                f"likely cause: {cluster['likely_cause']}",
                f"suggested action: {cluster['suggested_action']}",
                f"evidence: {evidence}",
                f"similar runs: {similar}",
            ]
        )
    return "\n".join(lines)
