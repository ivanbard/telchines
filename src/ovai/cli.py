from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import typer

from ovai.adapters.registry import AdapterRegistry
from ovai.config import ProjectConfig
from ovai.eval import run_default_suite
from ovai.models import VerificationRun
from ovai.providers import HeuristicRepairProvider
from ovai.retrieval import RetrievalService
from ovai.run_store import RunStore
from ovai.utils import dataclass_to_dict, stable_id, utc_now
from ovai.workflows.repair import execute_repair
from ovai.workflows.triage import triage_logs

app = typer.Typer(help="Open Verification AI Platform CLI", no_args_is_help=True, add_completion=False)
project_app = typer.Typer(no_args_is_help=True)
runs_app = typer.Typer(no_args_is_help=True)
eval_app = typer.Typer(no_args_is_help=True)
app.add_typer(project_app, name="project")
app.add_typer(runs_app, name="runs")
app.add_typer(eval_app, name="eval")


def _load_services(root: Path | None = None) -> tuple[ProjectConfig, RunStore, RetrievalService]:
    config = ProjectConfig.load(root or Path.cwd())
    store = RunStore(config)
    retrieval = RetrievalService(config)
    return config, store, retrieval


@project_app.command("init")
def project_init(path: Path = typer.Argument(Path(".")), name: Optional[str] = typer.Option(None, "--name")) -> None:
    config = ProjectConfig.init_project(path, name=name)
    typer.echo(f"initialized project {config.project.project_id} at {config.project.root_path}")


@app.command("index")
def index_project() -> None:
    _, _, retrieval = _load_services()
    chunk_count = retrieval.build_index()
    typer.echo(f"indexed {chunk_count} chunks")


@app.command("retrieve")
def retrieve(query: str, limit: int = typer.Option(5, "--limit")) -> None:
    _, store, retrieval = _load_services()
    context = retrieval.search(query, limit=limit)
    store.save_context(context)
    typer.echo(json.dumps({"context_id": context.context_id, "hits": [asdict(hit) for hit in context.hits]}, indent=2))


@runs_app.command("list")
def list_runs() -> None:
    _, store, _ = _load_services()
    typer.echo(json.dumps([dataclass_to_dict(run) for run in store.list_runs()], indent=2))


@runs_app.command("show")
def show_run(run_id: str) -> None:
    _, store, _ = _load_services()
    typer.echo(json.dumps(dataclass_to_dict(store.load_run(run_id)), indent=2))


@runs_app.command("replay")
def replay_run(run_id: str) -> None:
    config, store, _ = _load_services()
    run = store.load_run(run_id)
    if not run.replay_command:
        raise typer.BadParameter("run does not have a replay command")
    result = subprocess.run(run.replay_command, cwd=config.project_root, capture_output=True, text=True, check=False)
    typer.echo(json.dumps({"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}, indent=2))


@app.command("repair")
def repair(
    tool: str = typer.Option(..., "--tool"),
    files: list[str] = typer.Option(..., "--file"),
    extra_arg: list[str] = typer.Option([], "--extra-arg"),
    apply_patch: bool = typer.Option(False, "--apply"),
) -> None:
    config, store, retrieval = _load_services()
    registry = AdapterRegistry()
    adapter = registry.get(tool)
    run_id = stable_id("run", config.project.project_id, tool, utc_now(), ",".join(files))
    execution = adapter.run(run_id, config.project_root, files, config.project_root / config.artifacts_dir, extra_args=extra_arg)
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
        artifacts={"log_path": execution.log_path},
        observation_ids=[observation.observation_id for observation in execution.observations],
        summary=f"{tool} exited with code {execution.exit_code}",
        replay_command=execution.command,
    )
    store.save_run(base_run)
    proposal, validation_run, context = execute_repair(config, store, retrieval, HeuristicRepairProvider(), base_run, apply_patch=apply_patch)
    payload = {
        "run_id": base_run.run_id,
        "status": base_run.status,
        "context_id": context.context_id,
        "patch_id": proposal.patch_id if proposal else None,
        "validation_run_id": validation_run.run_id if validation_run else None,
        "validation_status": validation_run.status if validation_run else None,
    }
    typer.echo(json.dumps(payload, indent=2))


@app.command("triage")
def triage(logs: Path = typer.Option(..., "--logs")) -> None:
    config, store, retrieval = _load_services()
    run, clusters, context = triage_logs(config, store, retrieval, logs)
    typer.echo(
        json.dumps(
            {
                "run_id": run.run_id,
                "cluster_count": len(clusters),
                "context_id": context.context_id,
                "clusters": [dataclass_to_dict(cluster) for cluster in clusters],
            },
            indent=2,
        )
    )


@eval_app.command("run")
def eval_run() -> None:
    config, store, _ = _load_services()
    report = run_default_suite(config, store)
    typer.echo(json.dumps(report, indent=2))


@eval_app.command("report")
def eval_report() -> None:
    _, store, _ = _load_services()
    typer.echo(json.dumps(store.load_report("latest_eval"), indent=2))
