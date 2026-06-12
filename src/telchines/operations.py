from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

from telchines.adapters.registry import AdapterRegistry
from telchines.config import ProjectConfig
from telchines.eval import run_default_suite
from telchines.models import VerificationRun
from telchines.providers import build_generation_provider, build_repair_provider, check_provider_statuses, list_provider_statuses
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import SECRET_KEY_RE, dataclass_to_dict, ensure_directory, remove_tree, stable_id, utc_now
from telchines.workflows.coverage import execute_coverage_plan, format_coverage_human
from telchines.workflows.gen_cocotb import execute_cocotb_generation
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


def index_status(root: Path | None = None) -> dict[str, object]:
    _, _, retrieval = load_services(root)
    return retrieval.status()


def clean_index(root: Path | None = None) -> dict[str, object]:
    _, _, retrieval = load_services(root)
    return retrieval.clean()


def purge_artifacts(root: Path | None = None, *, dry_run: bool = True) -> dict[str, object]:
    config, store, _ = load_services(root)
    targets = [
        config.project_root / config.artifacts_dir,
        store.task_artifacts_dir,
        store.patches_dir,
        store.generations_dir,
        store.waveforms_dir,
        store.reports_dir,
    ]
    unique_targets = []
    seen: set[Path] = set()
    for target in targets:
        resolved = target.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_targets.append(target)
    summaries = [_directory_summary(target) for target in unique_targets]
    if not dry_run:
        for target in unique_targets:
            if target.exists():
                remove_tree(target)
            ensure_directory(target)
    return {
        "dry_run": dry_run,
        "targets": summaries,
        "file_count": sum(int(item["file_count"]) for item in summaries),
        "byte_count": sum(int(item["byte_count"]) for item in summaries),
        "status": "planned" if dry_run else "purged",
    }


def privacy_report(root: Path | None = None) -> dict[str, object]:
    config, _, _ = load_services(root)
    providers = config.project.model_policy.get("providers", {})
    risks: list[dict[str, object]] = []
    for name, provider_config in providers.items():
        if not isinstance(provider_config, dict):
            continue
        kind = provider_config.get("kind")
        if kind == "local_command":
            risks.append(
                {
                    "provider": name,
                    "severity": "warning",
                    "summary": "local_command providers execute configured local processes from the project root",
                }
            )
            env = provider_config.get("env", {})
            if isinstance(env, dict):
                secret_env_keys = [key for key, value in env.items() if SECRET_KEY_RE.search(str(key)) and str(value).strip()]
                if secret_env_keys:
                    risks.append(
                        {
                            "provider": name,
                            "severity": "warning",
                            "summary": f"provider env stores secret-looking keys in config: {', '.join(secret_env_keys)}",
                        }
                    )
        if kind == "openai_compatible" and not config.no_egress and config.model_mode != "local":
            risks.append(
                {
                    "provider": name,
                    "severity": "info",
                    "summary": "openai_compatible provider may send retrieved RTL/spec/log context to a configured HTTP endpoint",
                }
            )
    return {
        "model_mode": config.model_mode,
        "no_egress": config.no_egress,
        "artifact_dirs": [
            str(config.project_root / config.artifacts_dir),
            str(config.project_root / config.store_dir / "task-artifacts"),
        ],
        "risks": risks,
        "status": "warning" if any(item["severity"] == "warning" for item in risks) else "ok",
    }


def _directory_summary(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"path": str(path), "exists": False, "file_count": 0, "byte_count": 0}
    files = [item for item in path.rglob("*") if item.is_file()]
    return {
        "path": str(path),
        "exists": True,
        "file_count": len(files),
        "byte_count": sum(item.stat().st_size for item in files),
    }


def retrieve_query(root: Path | None, query: str, limit: int = 5, mode: str = "general") -> dict[str, object]:
    _, store, retrieval = load_services(root)
    context = retrieval.search(query, limit=limit, mode=mode)
    store.save_context(context)
    return {"context_id": context.context_id, "mode": context.mode, "hits": [asdict(hit) for hit in context.hits]}


def list_adapters(root: Path | None = None, category: str | None = None) -> dict[str, object]:
    config, _, _ = load_services(root)
    registry = AdapterRegistry()
    adapters = [adapter.describe(enabled=adapter.name in config.adapters) for adapter in registry.list(category=category)]
    return {"adapters": [dataclass_to_dict(adapter) for adapter in adapters]}


def check_adapters(root: Path | None = None, adapter_name: str | None = None, category: str | None = None) -> dict[str, object]:
    config, _, _ = load_services(root)
    registry = AdapterRegistry()
    if adapter_name:
        adapters = [registry.get(adapter_name)]
    else:
        adapters = registry.list(category=category)
    checks = []
    for adapter in adapters:
        descriptor = adapter.describe(enabled=adapter.name in config.adapters)
        missing = [binary for binary in descriptor.required_binaries if not _binary_available(binary)]
        checks.append(
            {
                "name": descriptor.name,
                "kind": descriptor.kind,
                "category": descriptor.category,
                "enabled": descriptor.enabled,
                "available": descriptor.available,
                "version": descriptor.version,
                "required_binaries": descriptor.required_binaries,
                "missing_binaries": missing,
                "status": "passed" if descriptor.available else "missing",
                "summary": "adapter is available" if descriptor.available else f"missing required binaries: {', '.join(missing)}",
            }
        )
    return {"adapters": checks}


def _binary_available(binary: str) -> bool:
    return shutil.which(binary) is not None


def list_runs(root: Path | None = None) -> list[dict[str, object]]:
    _, store, _ = load_services(root)
    return [dataclass_to_dict(run) for run in store.list_runs()]


def show_run(root: Path | None, run_id: str) -> dict[str, object]:
    _, store, _ = load_services(root)
    return dataclass_to_dict(store.load_run(run_id))


def replay_run(root: Path | None, run_id: str, *, confirm: bool = False) -> dict[str, object]:
    config, store, _ = load_services(root)
    run = store.load_run(run_id)
    if not run.replay_command:
        raise ValueError("run does not have a replay command")
    if not confirm:
        return {
            "status": "confirmation_required",
            "run_id": run.run_id,
            "workflow_type": run.workflow_type,
            "replay_command": run.replay_command,
            "summary": "replay command was not executed; rerun with --yes to execute stored command",
        }
    result = subprocess.run(run.replay_command, cwd=config.project_root, capture_output=True, text=True, check=False)
    return {
        "status": "executed",
        "run_id": run.run_id,
        "workflow_type": run.workflow_type,
        "replay_command": run.replay_command,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


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
        inputs={"files": files, "project_root": str(config.project_root), "extra_args": extra_arg, "tool_name": tool},
        status="passed" if execution.exit_code == 0 else "failed",
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        exit_code=execution.exit_code,
        artifacts=execution.artifacts,
        tool_result=execution.result,
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
        "validation_mode": validation_run.tool_result.get("validation_mode") if validation_run else None,
        "validation_limitations": validation_run.tool_result.get("limitations", []) if validation_run else [],
    }


def gen_cocotb(
    root: Path | None,
    dut: Path,
    spec: Path | None = None,
    output_dir: Path | None = None,
    intent: str = "",
    provider_name: str | None = None,
) -> dict[str, object]:
    config, store, retrieval = load_services(root)
    dut_path = dut if dut.is_absolute() else (config.project_root / dut).resolve()
    spec_path = None if spec is None else (spec if spec.is_absolute() else (config.project_root / spec).resolve())
    target_output_dir = None if output_dir is None else (output_dir if output_dir.is_absolute() else (config.project_root / output_dir).resolve())
    provider = build_generation_provider(config, provider_name=provider_name)
    candidate, run, validation_run, context = execute_cocotb_generation(
        config,
        store,
        retrieval,
        provider,
        dut_path,
        spec_path=spec_path,
        output_dir=target_output_dir,
        intent=intent,
    )
    return {
        "context_id": context.context_id,
        "run_id": run.run_id if run else None,
        "candidate_id": candidate.candidate_id if candidate else None,
        "provider": candidate.provider if candidate else getattr(provider, "name", ""),
        "status": candidate.status if candidate else "no_generation",
        "artifact_path": candidate.file_path if candidate else None,
        "manifest_path": candidate.manifest_path if candidate else None,
        "dut_path": candidate.dut_path if candidate else str(dut_path.relative_to(config.project_root)).replace("\\", "/"),
        "spec_path": candidate.spec_path if candidate else (str(spec_path.relative_to(config.project_root)).replace("\\", "/") if spec_path else None),
        "top_module": candidate.top_module if candidate else None,
        "intent": candidate.intent if candidate else intent,
        "explanation": candidate.explanation if candidate else None,
        "assumptions": candidate.assumptions if candidate else [],
        "ports": [dataclass_to_dict(item) for item in candidate.ports] if candidate else [],
        "evidence_paths": candidate.evidence_paths if candidate else [],
        "replay_artifacts": candidate.replay_artifacts if candidate else {},
        "validation_run_id": validation_run.run_id if validation_run else None,
        "validation_status": validation_run.status if validation_run else None,
        "validation_summary": validation_run.summary if validation_run else None,
        "validation_mode": validation_run.tool_result.get("validation_mode") if validation_run else None,
        "validation_limitations": validation_run.tool_result.get("limitations", []) if validation_run else [],
    }


def coverage_plan(
    root: Path | None,
    report: Path,
    *,
    exclusions: Path | None = None,
    formal_run_id: str | None = None,
    rtl: list[Path] | None = None,
    spec: list[Path] | None = None,
) -> dict[str, object]:
    config, store, retrieval = load_services(root)
    report_path = report if report.is_absolute() else (config.project_root / report).resolve()
    exclusions_path = None if exclusions is None else (exclusions if exclusions.is_absolute() else (config.project_root / exclusions).resolve())
    rtl_paths = [(path if path.is_absolute() else (config.project_root / path).resolve()) for path in (rtl or [])]
    spec_paths = [(path if path.is_absolute() else (config.project_root / path).resolve()) for path in (spec or [])]
    plan, run, context = execute_coverage_plan(
        config,
        store,
        retrieval,
        report_path,
        exclusions_path=exclusions_path,
        formal_run_id=formal_run_id,
        rtl_paths=rtl_paths,
        spec_paths=spec_paths,
    )
    return {
        "run_id": run.run_id,
        "context_id": context.context_id,
        "plan_id": plan.plan_id,
        "report_path": plan.report_path,
        "exclusions_path": plan.exclusions_path,
        "formal_run_id": plan.formal_run_id,
        "recommendation_count": len(plan.recommendations),
        "excluded_count": len(plan.excluded_item_ids),
        "summary": plan.summary,
        "focus_paths": plan.focus_paths,
        "recommendations": [dataclass_to_dict(item) for item in plan.recommendations],
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


def check_providers(root: Path | None = None, provider_name: str | None = None, *, live: bool = True) -> dict[str, object]:
    config, _, _ = load_services(root)
    checks = [dataclass_to_dict(check) for check in check_provider_statuses(config, provider_name, live=live)]
    return {
        "live": live,
        "providers": checks,
        "status": "passed" if all(item["status"] == "passed" for item in checks) else "failed",
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
                "formal_evidence": [
                    {
                        "run_id": item["run_id"],
                        "status": item["status"],
                        "summary": item["summary"],
                        "property_ids": item["property_ids"],
                        "counterexample_paths": item["counterexample_paths"],
                        "report_paths": item["report_paths"],
                    }
                    for item in cluster.get("formal_evidence", [])
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
        formal = ", ".join(
            f"{item['run_id']} [{item['status']}]"
            + (f" props={', '.join(item['property_ids'][:2])}" if item.get("property_ids") else "")
            for item in cluster.get("formal_evidence", [])[:2]
        ) or "none"
        lines.extend(
            [
                "",
                f"{index}. {cluster['summary']}",
                f"likely cause: {cluster['likely_cause']}",
                f"suggested action: {cluster['suggested_action']}",
                f"evidence: {evidence}",
                f"formal: {formal}",
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
