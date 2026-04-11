from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from telchines.adapters.registry import AdapterRegistry
from telchines.config import ProjectConfig
from telchines.eval import run_default_suite
from telchines.models import VerificationRun
from telchines.providers import build_generation_provider, build_repair_provider, list_provider_statuses
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import dataclass_to_dict, stable_id, utc_now
from telchines.workflows.gen_sva import execute_generation
from telchines.workflows.repair import execute_repair
from telchines.workflows.triage import triage_logs
from telchines.waveforms import ingest_waveform, select_signal


def load_services(root: Path | None = None) -> tuple[ProjectConfig, RunStore, RetrievalService]:
    config = ProjectConfig.discover(root or Path.cwd())
    store = RunStore(config)
    retrieval = RetrievalService(config)
    return config, store, retrieval


def initialize_project(path: Path, name: str | None = None) -> ProjectConfig:
    return ProjectConfig.init_project(path, name=name)


def index_project(root: Path | None = None) -> int:
    _, _, retrieval = load_services(root)
    return retrieval.build_index()


def retrieve_query(root: Path | None, query: str, limit: int = 5, mode: str = "general") -> dict[str, object]:
    _, store, retrieval = load_services(root)
    context = retrieval.search(query, limit=limit, mode=mode)
    store.save_context(context)
    return {"context_id": context.context_id, "mode": context.mode, "hits": [asdict(hit) for hit in context.hits]}


def list_runs(root: Path | None = None) -> list[dict[str, object]]:
    _, store, _ = load_services(root)
    return [dataclass_to_dict(run) for run in store.list_runs()]


def show_run(root: Path | None, run_id: str) -> dict[str, object]:
    _, store, _ = load_services(root)
    return dataclass_to_dict(store.load_run(run_id))


def replay_run(root: Path | None, run_id: str) -> dict[str, object]:
    config, store, _ = load_services(root)
    run = store.load_run(run_id)
    if not run.replay_command:
        raise ValueError("run does not have a replay command")
    result = subprocess.run(run.replay_command, cwd=config.project_root, capture_output=True, text=True, check=False)
    return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def repair(root: Path | None, tool: str, files: list[str], extra_arg: list[str] | None = None, apply_patch: bool = False) -> dict[str, object]:
    config, store, retrieval = load_services(root)
    adapter = AdapterRegistry().get(tool)
    extra_arg = extra_arg or []
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
        exit_code=execution.exit_code,
        artifacts={"log_path": execution.log_path},
        observation_ids=[observation.observation_id for observation in execution.observations],
        summary=execution.summary,
        replay_command=execution.command,
    )
    store.save_run(base_run)
    provider = build_repair_provider(config)
    proposal, validation_run, context = execute_repair(config, store, retrieval, provider, base_run, apply_patch=apply_patch)
    return {
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


def gen_sva(
    root: Path | None,
    spec: Path,
    rtl: Path,
    output: Path | None = None,
    provider_name: str | None = None,
) -> dict[str, object]:
    config, store, retrieval = load_services(root)
    spec_path = spec if spec.is_absolute() else (config.project_root / spec).resolve()
    rtl_path = rtl if rtl.is_absolute() else (config.project_root / rtl).resolve()
    output_path = None if output is None else (output if output.is_absolute() else (config.project_root / output).resolve())
    provider = build_generation_provider(config, provider_name=provider_name)
    candidate, validation_run, context = execute_generation(config, store, retrieval, provider, spec_path, rtl_path, output_path=output_path)
    return {
        "context_id": context.context_id,
        "candidate_id": candidate.candidate_id if candidate else None,
        "provider": candidate.provider if candidate else getattr(provider, "name", ""),
        "status": candidate.status if candidate else "no_generation",
        "artifact_path": candidate.file_path if candidate else None,
        "spec_path": candidate.spec_path if candidate else str(spec_path.relative_to(config.project_root)).replace("\\", "/"),
        "rtl_path": candidate.rtl_path if candidate else str(rtl_path.relative_to(config.project_root)).replace("\\", "/"),
        "explanation": candidate.explanation if candidate else None,
        "property_summaries": [dataclass_to_dict(item) for item in candidate.properties] if candidate else [],
        "evidence_paths": candidate.evidence_paths if candidate else [],
        "replay_artifacts": candidate.replay_artifacts if candidate else {},
        "validation_run_id": validation_run.run_id if validation_run else None,
        "validation_status": validation_run.status if validation_run else None,
        "validation_summary": validation_run.summary if validation_run else None,
    }


def triage(root: Path | None, logs: list[Path], waveforms: list[Path] | None = None) -> dict[str, object]:
    config, store, retrieval = load_services(root)
    run, clusters, context = triage_logs(config, store, retrieval, logs, waveform_paths=waveforms)
    return {
        "run_id": run.run_id,
        "cluster_count": len(clusters),
        "context_id": context.context_id,
        "waveform_count": int(run.inputs.get("waveform_count", 0)),
        "clusters": [dataclass_to_dict(cluster) for cluster in clusters],
    }


def list_providers(root: Path | None = None) -> dict[str, object]:
    config, _, _ = load_services(root)
    return {
        "default_provider_by_capability": config.default_provider_by_capability(),
        "providers": [
            {
                "name": status.name,
                "kind": status.kind,
                "capabilities": status.capabilities,
                "default_for": status.default_for,
                "allowed": status.allowed,
                "blocked_reason": status.blocked_reason or None,
            }
            for status in list_provider_statuses(config)
        ],
    }


def run_eval(root: Path | None = None) -> dict[str, object]:
    config, store, _ = load_services(root)
    return run_default_suite(config, store)


def load_eval_report(root: Path | None = None) -> dict[str, object]:
    _, store, _ = load_services(root)
    return store.load_report("latest_eval")


def list_waveforms(root: Path | None = None) -> dict[str, object]:
    _, store, _ = load_services(root)
    return {
        "waveforms": [dataclass_to_dict(summary) for summary in store.list_waveform_summaries()],
    }


def show_waveform(root: Path | None, target: str) -> dict[str, object]:
    config, store, _ = load_services(root)
    summary = _resolve_waveform_summary(config, store, root or config.project_root, target)
    return dataclass_to_dict(summary)


def waveform_signals(root: Path | None, target: str, signal_filter: str | None = None) -> dict[str, object]:
    config, store, _ = load_services(root)
    summary = _resolve_waveform_summary(config, store, root or config.project_root, target)
    signals = [dataclass_to_dict(item) for item in summary.signals]
    if signal_filter:
        lowered = signal_filter.lower()
        signals = [item for item in signals if lowered in item["full_name"].lower() or lowered in item["name"].lower()]
    return {
        "waveform_id": summary.waveform_id,
        "source_path": summary.source_path,
        "signal_count": len(signals),
        "signals": signals,
    }


def inspect_waveform(root: Path | None, target: str, signal: str, window: int = 8) -> dict[str, object]:
    config, store, _ = load_services(root)
    summary = _resolve_waveform_summary(config, store, root or config.project_root, target)
    sample = select_signal(summary, signal)
    transitions = sample.transitions[: max(window, 1)]
    return {
        "waveform_id": summary.waveform_id,
        "source_path": summary.source_path,
        "signal_name": sample.signal_name,
        "full_name": sample.full_name,
        "timescale": summary.timescale,
        "transition_count": len(sample.transitions),
        "transitions": [dataclass_to_dict(item) for item in transitions],
    }


def format_triage_ci(payload: dict[str, object]) -> dict[str, object]:
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
                "waveforms": [
                    {
                        "waveform_id": item["waveform_id"],
                        "source_path": item["source_path"],
                        "matched_signals": item["matched_signals"],
                    }
                    for item in cluster["waveform_evidence"]
                ],
                "similar_runs": [match["run_id"] for match in cluster["similar_runs"]],
            }
            for cluster in clusters
        ],
    }


def format_triage_human(payload: dict[str, object]) -> str:
    lines = [f"run {payload['run_id']} produced {payload['cluster_count']} cluster(s)"]
    for index, cluster in enumerate(payload["clusters"], start=1):
        evidence = ", ".join(hit["citation"] for hit in cluster["evidence_hits"][:3]) or "none"
        similar = ", ".join(match["run_id"] for match in cluster["similar_runs"]) or "none"
        waveforms = ", ".join(
            f"{item['source_path']} ({', '.join(item['matched_signals']) or 'signals unavailable'})"
            for item in cluster["waveform_evidence"][:2]
        ) or "none"
        lines.extend(
            [
                "",
                f"{index}. {cluster['summary']}",
                f"likely cause: {cluster['likely_cause']}",
                f"suggested action: {cluster['suggested_action']}",
                f"evidence: {evidence}",
                f"waveforms: {waveforms}",
                f"similar runs: {similar}",
            ]
        )
    return "\n".join(lines)


def dump_json(value: object) -> str:
    return json.dumps(value, indent=2)


def _resolve_waveform_summary(config: ProjectConfig, store: RunStore, working_root: Path, target: str):
    waveform_path = store.waveforms_dir / f"{target}.json"
    if waveform_path.exists():
        return store.load_waveform_summary(target)
    try:
        run = store.load_run(target)
    except FileNotFoundError:
        run = None
    if run is not None:
        waveform_ids = [item for item in run.artifacts.get("waveform_ids", "").split(",") if item]
        if not waveform_ids:
            raise ValueError(f"run does not have linked waveforms: {target}")
        return store.load_waveform_summary(waveform_ids[0])
    candidate = Path(target)
    resolved = candidate if candidate.is_absolute() else (working_root / candidate)
    summary = ingest_waveform(config, store, resolved)
    return summary
