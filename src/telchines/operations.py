from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from telchines.adapters.base import AdapterRunSpec
from telchines.ci_importers import import_ci_runs
from telchines.coverage_import import import_coverage_report
from telchines.errors import ConfigError, ProjectNotInitializedError, WorkflowInputError
from telchines.adapters.registry import AdapterRegistry
from telchines.config import ProjectConfig
from telchines.eval import run_default_suite
from telchines.import_manifest import import_regression_manifest
from telchines.model_catalog import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_OPENAI_API_MODEL,
    list_model_options as list_model_options_catalog,
    set_default_provider as set_default_provider_catalog,
    set_provider_model as set_provider_model_catalog,
    set_provider_reasoning as set_provider_reasoning_catalog,
    provider_model_metadata,
)
from telchines.models import VerificationRun
from telchines.providers import _provider_network_scope, build_generation_provider, build_repair_provider, check_provider_statuses, list_provider_statuses
from telchines.project_templates import apply_project_template, list_project_templates
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import SECRET_KEY_RE, dataclass_to_dict, ensure_directory, read_json, remove_tree, stable_id, utc_now
from telchines.workflows.agent import execute_agent
from telchines.workflows.coverage import execute_coverage_plan, format_coverage_human  # noqa: F401
from telchines.workflows.gen_cocotb import execute_cocotb_generation
from telchines.workflows.gen_sva import execute_generation
from telchines.workflows.repair import execute_repair
from telchines.workflows.triage import triage_logs
from telchines.waveforms import correlate_log_timestamps, ingest_waveform, match_signal, summarize_signal_window

ARTIFACT_PURGE_SCOPES = ("generated", "task-artifacts", "patches", "generations", "waveforms", "reports")
ENV_VAR_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def load_services(root: Path | None = None) -> tuple[ProjectConfig, RunStore, RetrievalService]:
    config = ProjectConfig.discover(root or Path.cwd())
    store = RunStore(config)
    retrieval = RetrievalService(config)
    return config, store, retrieval


def initialize_project(path: Path, name: str | None = None, template: str | None = None) -> ProjectConfig:
    config = ProjectConfig.init_project(path, name=name)
    if template:
        apply_project_template(config, template)
        config = ProjectConfig.load(config.project_root)
    return config


def project_templates() -> dict[str, object]:
    templates = list_project_templates()
    return {"templates": templates, "template_count": len(templates)}


def index_project(root: Path | None = None) -> int:
    _, _, retrieval = load_services(root)
    return retrieval.build_index()


def index_status(root: Path | None = None) -> dict[str, object]:
    _, _, retrieval = load_services(root)
    return retrieval.status()


def clean_index(root: Path | None = None) -> dict[str, object]:
    _, _, retrieval = load_services(root)
    return retrieval.clean()


def purge_artifacts(
    root: Path | None = None,
    *,
    dry_run: bool = True,
    scopes: list[str] | None = None,
    older_than_days: int | None = None,
) -> dict[str, object]:
    config, store, _ = load_services(root)
    if older_than_days is not None and older_than_days < 0:
        raise ValueError("older_than_days must be zero or greater")
    selected_scopes = _normalize_artifact_purge_scopes(scopes)
    targets_by_scope = {
        "generated": config.project_root / config.artifacts_dir,
        "task-artifacts": store.task_artifacts_dir,
        "patches": store.patches_dir,
        "generations": store.generations_dir,
        "waveforms": store.waveforms_dir,
        "reports": store.reports_dir,
    }
    cutoff = None if older_than_days is None else time.time() - (older_than_days * 24 * 60 * 60)
    unique_targets: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for scope in selected_scopes:
        target = targets_by_scope[scope]
        resolved = target.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_targets.append((scope, target))
    summaries = [_directory_summary(target, scope=scope, cutoff=cutoff) for scope, target in unique_targets]
    if not dry_run:
        for summary, (_, target) in zip(summaries, unique_targets, strict=True):
            if older_than_days is None and target.exists():
                remove_tree(target)
                ensure_directory(target)
                continue
            for file_path in summary["files"]:
                candidate = Path(str(file_path))
                if candidate.exists() and candidate.is_file():
                    candidate.unlink()
    return {
        "dry_run": dry_run,
        "scopes": selected_scopes,
        "older_than_days": older_than_days,
        "targets": summaries,
        "file_count": sum(int(item["file_count"]) for item in summaries),
        "byte_count": sum(int(item["byte_count"]) for item in summaries),
        "status": "planned" if dry_run else "purged",
        "retained_metadata": [
            "run records under .tel/runs",
            "retrieval contexts under .tel/contexts",
            "observations under .tel/observations",
            "project config under .tel/config.json",
        ],
        "privacy_note": "Purge removes artifact payloads but does not redact or rewrite preserved run metadata, context records, observations, or workspace files.",
    }


def review_artifact(root: Path | None = None, reference: str = "", *, max_diff_lines: int = 200) -> dict[str, object]:
    config, store, _ = load_services(root)
    max_diff_lines = max(1, max_diff_lines)
    candidate = _resolve_generation_candidate(store, reference)
    file_path = str(candidate.get("file_path", ""))
    if not file_path:
        raise ValueError(f"generation candidate does not have a generated file path: {reference}")
    generated_path = _safe_project_path(config, file_path)
    baseline = str(candidate.get("candidate_content", ""))
    current = generated_path.read_text(encoding="utf-8") if generated_path.exists() else ""
    diff_lines = list(
        difflib.unified_diff(
            baseline.splitlines(),
            current.splitlines(),
            fromfile=f"stored:{file_path}",
            tofile=f"workspace:{file_path}",
            lineterm="",
        )
    )
    truncated = len(diff_lines) > max_diff_lines
    if truncated:
        diff_lines = diff_lines[:max_diff_lines]
    status = "missing" if not generated_path.exists() else "unchanged" if baseline == current else "modified"
    validation_attempts = candidate.get("validation_attempts", [])
    return {
        "reference": reference,
        "candidate_id": candidate.get("candidate_id"),
        "workflow_type": candidate.get("workflow_type", _generation_workflow_type(candidate)),
        "provider": candidate.get("provider"),
        "status": status,
        "generated_file": file_path,
        "exists": generated_path.exists(),
        "baseline_line_count": len(baseline.splitlines()),
        "current_line_count": len(current.splitlines()) if generated_path.exists() else 0,
        "diff_line_count": len(diff_lines),
        "diff_truncated": truncated,
        "diff": "\n".join(diff_lines),
        "validation_attempts": validation_attempts,
        "attempts": candidate.get("attempts", []),
        "rejected_candidate_ids": candidate.get("rejected_candidate_ids", []),
        "evidence_paths": candidate.get("evidence_paths", []),
        "replay_artifacts": candidate.get("replay_artifacts", {}),
        "summary": _artifact_review_summary(status, file_path, len(diff_lines), truncated),
    }


def privacy_report(root: Path | None = None) -> dict[str, object]:
    config, _, _ = load_services(root)
    providers = config.project.model_policy.get("providers", {})
    risks: list[dict[str, object]] = []
    for name, provider_config in providers.items():
        if not isinstance(provider_config, dict):
            continue
        kind = provider_config.get("kind")
        effective_config = provider_config
        effective_kind = kind
        if kind == "agent_runtime":
            base_provider = provider_config.get("base_provider")
            base_config = providers.get(str(base_provider))
            if isinstance(base_config, dict):
                risks.append(
                    {
                        "provider": name,
                        "severity": "info",
                        "summary": f"agent_runtime provider delegates repair proposals to base provider {base_provider}",
                    }
                )
                effective_config = base_config
                effective_kind = base_config.get("kind")
        if effective_kind == "local_command":
            risks.append(
                {
                    "provider": name,
                    "severity": "warning",
                    "summary": "local_command providers execute configured local processes from the project root",
                }
            )
            env = effective_config.get("env", {})
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
        if (
            effective_kind in {"openai_compatible", "anthropic"}
            and _provider_network_scope(effective_config, providers) == "external_http"
            and not config.no_egress
            and config.model_mode != "local"
        ):
            risks.append(
                {
                    "provider": name,
                    "severity": "info",
                    "summary": f"{effective_kind} provider may send retrieved RTL/spec/log context to a configured HTTP endpoint",
                }
            )
    return {
        "model_mode": config.model_mode,
        "no_egress": config.no_egress,
        "artifact_dirs": [
            str(config.project_root / config.artifacts_dir),
            str(config.project_root / config.store_dir / "task-artifacts"),
        ],
        "artifact_scopes": list(ARTIFACT_PURGE_SCOPES),
        "artifact_retention": {
            "default": "kept until explicitly purged",
            "purge_preview": "tel artifacts purge",
            "purge_all": "tel artifacts purge --yes",
            "purge_by_scope": "tel artifacts purge --scope task-artifacts --scope reports --yes",
            "purge_by_age": "tel artifacts purge --older-than-days 30 --yes",
            "preserved_metadata": [
                ".tel/runs",
                ".tel/contexts",
                ".tel/observations",
                ".tel/config.json",
            ],
        },
        "retention_guidance": [
            "Telchines keeps generated artifacts, patch metadata, reports, waveform summaries, replay metadata, and task artifacts under the project .tel directory.",
            "Task artifacts intentionally retain prompts, retrieved RTL/spec/log snippets, and provider responses for replay and auditability.",
            "Run `tel artifacts purge` to preview cleanup and `tel artifacts purge --yes` to remove retained artifact payloads.",
            "Use `--scope` or `--older-than-days` to enforce narrower artifact-retention windows without deleting run metadata.",
        ],
        "cleanup_command": "tel artifacts purge",
        "redaction_scope": "Dictionary fields with credential-looking keys are redacted before task artifacts are stored; proprietary RTL/spec/log content is not redacted.",
        "remote_context_warning": "Remote HTTP providers may receive retrieved RTL/spec/log context unless blocked by model_mode=local or no_egress=true.",
        "risks": risks,
        "status": "warning" if any(item["severity"] == "warning" for item in risks) else "ok",
    }


def _normalize_artifact_purge_scopes(scopes: list[str] | None) -> list[str]:
    raw = scopes or list(ARTIFACT_PURGE_SCOPES)
    normalized: list[str] = []
    for scope in raw:
        value = scope.strip()
        if value not in ARTIFACT_PURGE_SCOPES:
            raise ValueError(f"artifact purge scope must be one of: {', '.join(ARTIFACT_PURGE_SCOPES)}")
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise ValueError("at least one artifact purge scope is required")
    return normalized


def _directory_summary(path: Path, *, scope: str | None = None, cutoff: float | None = None) -> dict[str, object]:
    if not path.exists():
        return {"scope": scope, "path": str(path), "exists": False, "file_count": 0, "byte_count": 0, "files": []}
    files = [item for item in path.rglob("*") if item.is_file() and (cutoff is None or item.stat().st_mtime <= cutoff)]
    return {
        "scope": scope,
        "path": str(path),
        "exists": True,
        "file_count": len(files),
        "byte_count": sum(item.stat().st_size for item in files),
        "files": [str(item) for item in files],
    }


def _resolve_generation_candidate(store: RunStore, reference: str) -> dict[str, object]:
    if not reference.strip():
        raise ValueError("artifact review requires a candidate id or validation run id")
    for path in sorted(store.generations_dir.glob("*.json")):
        payload = read_json(path)
        if payload.get("candidate_id") == reference:
            return payload
        if payload.get("file_path") == reference or payload.get("manifest_path") == reference:
            return payload
        validation_attempts = payload.get("validation_attempts", [])
        if isinstance(validation_attempts, list) and any(item.get("run_id") == reference for item in validation_attempts if isinstance(item, dict)):
            return payload
    raise ValueError(f"no generated artifact found for reference: {reference}")


def _safe_project_path(config: ProjectConfig, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ValueError(f"generated artifact path must be relative to the project root: {relative_path}")
    resolved = (config.project_root / candidate).resolve()
    try:
        resolved.relative_to(config.project_root.resolve())
    except ValueError as exc:
        raise ValueError(f"generated artifact path escapes the project root: {relative_path}") from exc
    return resolved


def _generation_workflow_type(candidate: dict[str, object]) -> str:
    if "manifest_path" in candidate:
        return "dut_to_cocotb"
    if "properties" in candidate:
        return "spec_to_sva"
    return "generation"


def _artifact_review_summary(status: str, file_path: str, diff_line_count: int, truncated: bool) -> str:
    if status == "missing":
        return f"generated artifact is missing from workspace: {file_path}"
    if status == "unchanged":
        return f"generated artifact matches stored candidate content: {file_path}"
    suffix = " (truncated)" if truncated else ""
    return f"generated artifact has workspace edits: {file_path}; {diff_line_count} diff line(s){suffix}"


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
        missing = adapter.missing_binaries()
        preview_spec = AdapterRunSpec(files=["rtl/example.sv"], top_module="example_top")
        try:
            command_preview = adapter.build_command_from_spec(config.project_root, preview_spec)
        except Exception:
            command_preview = adapter.build_command(config.project_root, ["rtl/example.sv"], [])
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
                "status": "passed" if descriptor.available else "unavailable",
                "summary": "adapter is available" if descriptor.available else f"unavailable required binaries: {', '.join(missing) or descriptor.name}",
                "command_preview": command_preview,
                "timeout_default_seconds": None,
                "setup_diagnostics": [] if descriptor.available else adapter.setup_diagnostics(missing),
            }
        )
    return {"adapters": checks}


def _binary_available(binary: str) -> bool:
    return shutil.which(binary) is not None


def list_runs(root: Path | None = None) -> list[dict[str, object]]:
    _, store, _ = load_services(root)
    return [dataclass_to_dict(run) for run in store.list_runs()]


def doctor_runs(root: Path | None = None) -> dict[str, object]:
    _, store, _ = load_services(root)
    runs = store.list_runs()
    issues = store.list_run_load_issues()
    return {
        "status": "passed" if not issues else "warning",
        "run_count": len(runs),
        "issue_count": len(issues),
        "issues": issues,
    }


def doctor_summary(root: Path | None = None) -> dict[str, object]:
    working_root = root or Path.cwd()
    try:
        config, _, _ = load_services(working_root)
    except ProjectNotInitializedError:
        return {"status": "not_initialized", "project": None, "next_action": "Run `tel get-started` or `tel project init .`."}
    index = index_status(config.project_root)
    providers = check_providers(config.project_root, live=False)
    adapters = list_adapters(config.project_root)
    available_adapters = sum(1 for item in adapters["adapters"] if item.get("available"))
    project_index = index["project"]
    status = "ready" if not project_index["stale"] and providers["status"] == "passed" else "needs_attention"
    next_action = "Run `tel index` to refresh project context." if project_index["stale"] else "Run `tel get-started` to choose the next workflow."
    return {
        "status": status,
        "project": {"name": config.project.name, "root": str(config.project_root)},
        "index": project_index,
        "providers": {"status": providers["status"]},
        "adapters": {"available": available_adapters, "total": len(adapters["adapters"])},
        "artifacts_dir": str(config.project_root / config.artifacts_dir),
        "next_action": next_action,
    }


def show_run(root: Path | None, run_id: str) -> dict[str, object]:
    _, store, _ = load_services(root)
    return dataclass_to_dict(store.load_run(run_id))


def replay_run(root: Path | None, run_id: str, *, confirm: bool = False) -> dict[str, object]:
    config, store, _ = load_services(root)
    try:
        run = store.load_run(run_id)
    except FileNotFoundError as exc:
        raise ValueError(f"run {run_id} does not exist") from exc
    replayability = _replayability(run)
    if not run.replay_command:
        raise ValueError(f"run {run.run_id} is not replayable: {replayability['reason']}")
    if not confirm:
        return {
            "status": "confirmation_required",
            "run_id": run.run_id,
            "workflow_type": run.workflow_type,
            "replayability": replayability,
            "replay_command": run.replay_command,
            "summary": "replay command was not executed; rerun with --yes to execute stored command",
        }
    try:
        result = subprocess.run(run.replay_command, cwd=config.project_root, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        executable = run.replay_command[0]
        raise ValueError(f"run {run.run_id} replay command is unavailable: executable `{executable}` was not found") from exc
    return {
        "status": "executed",
        "run_id": run.run_id,
        "workflow_type": run.workflow_type,
        "replayability": replayability,
        "replay_command": run.replay_command,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def import_runs(root: Path | None, manifest: Path, *, dry_run: bool = False) -> dict[str, object]:
    config, store, _ = load_services(root)
    payload = import_regression_manifest(config, store, manifest, dry_run=dry_run)
    for item in payload.get("runs", []):
        if not isinstance(item, dict):
            continue
        if dry_run:
            run_payload = item.get("run")
            command = run_payload.get("replay_command", []) if isinstance(run_payload, dict) else []
            item["replayability"] = _replayability_from_command(command, imported=True)
        else:
            run_id = item.get("run_id")
            if isinstance(run_id, str):
                item["replayability"] = _replayability(store.load_run(run_id))
    return payload


def import_runs_from_ci(root: Path | None, source: Path, *, importer: str, dry_run: bool = False) -> dict[str, object]:
    config, store, _ = load_services(root)
    return import_ci_runs(config, store, source, importer=importer, dry_run=dry_run)


def repair(
    root: Path | None,
    tool: str,
    files: list[str],
    extra_arg: list[str] | None = None,
    apply_patch: bool = False,
    *,
    filelists: list[str] | None = None,
    include_dirs: list[str] | None = None,
    defines: list[str] | None = None,
    top_module: str | None = None,
    work_library: str | None = None,
    adapter_args: list[str] | None = None,
) -> dict[str, object]:
    config, store, retrieval = load_services(root)
    adapter = AdapterRegistry().get(tool)
    extra_arg = [*(extra_arg or []), *(adapter_args or [])]
    run_spec = _adapter_run_spec(
        files=files,
        filelists=filelists,
        include_dirs=include_dirs,
        defines=defines,
        top_module=top_module,
        work_library=work_library,
        extra_args=extra_arg,
    )
    run_id = stable_id("run", config.project.project_id, tool, utc_now(), ",".join(files or filelists or []))
    execution = adapter.run(run_id, config.project_root, files, config.project_root / config.artifacts_dir, extra_args=extra_arg, spec=run_spec)
    store.save_observations(execution.observations)
    base_run = VerificationRun(
        run_id=run_id,
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="compile_repair",
        tool=adapter.tool_reference,
        inputs={
            "files": run_spec.expanded(config.project_root).files,
            "project_root": str(config.project_root),
            "extra_args": extra_arg,
            "tool_name": tool,
            "run_spec": run_spec.summary(config.project_root),
        },
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
    workflow_status = _repair_workflow_status(proposal.status if proposal else "no_patch", validation_run.status if validation_run else None, apply_patch=apply_patch)
    review_status = _repair_review_status(workflow_status)
    validation_mode = _validation_mode(validation_run)
    return {
        "run_id": base_run.run_id,
        "status": workflow_status,
        "workflow_status": workflow_status,
        "initial_tool_status": base_run.status,
        "candidate_status": proposal.status if proposal else "no_patch",
        "review_status": review_status,
        "context_id": context.context_id,
        "patch_id": proposal.patch_id if proposal else None,
        "provider": proposal.provider if proposal else getattr(provider, "name", ""),
        "proposal_explanation": proposal.explanation if proposal else None,
        "evidence_paths": proposal.evidence_paths if proposal else [],
        "replay_artifacts": proposal.replay_artifacts if proposal else {},
        "runtime_mode": proposal.runtime_mode if proposal else None,
        "runtime_available": proposal.runtime_available if proposal else None,
        "runtime_reason": proposal.runtime_reason if proposal else None,
        "validation_run_id": validation_run.run_id if validation_run else None,
        "validation_status": validation_run.status if validation_run else None,
        "validation_summary": validation_run.summary if validation_run else None,
        "validation_mode": validation_mode,
        "validation_scope": validation_mode,
    }


def agent(
    root: Path | None,
    task: str,
    *,
    tool: str | None = None,
    files: list[str] | None = None,
    extra_arg: list[str] | None = None,
    apply_patch: bool = False,
    logs: list[Path] | None = None,
    waveforms: list[Path] | None = None,
    report: Path | None = None,
    exclusions: Path | None = None,
    formal_run_id: str | None = None,
    rtl: list[Path] | None = None,
    spec: list[Path] | None = None,
    dut: Path | None = None,
    output: Path | None = None,
    output_dir: Path | None = None,
    provider_name: str | None = None,
    intent: str = "",
    filelists: list[str] | None = None,
    include_dirs: list[str] | None = None,
    defines: list[str] | None = None,
    top_module: str | None = None,
    work_library: str | None = None,
    adapter_args: list[str] | None = None,
) -> dict[str, object]:
    config, store, retrieval = load_services(root)
    _validate_agent_inputs(config, logs=logs, waveforms=waveforms, report=report, exclusions=exclusions, rtl=rtl, spec=spec, dut=dut)
    return execute_agent(
        config,
        store,
        retrieval,
        task,
        tool=tool,
        files=files,
        extra_args=extra_arg,
        apply_patch=apply_patch,
        logs=logs,
        waveforms=waveforms,
        report=report,
        exclusions=exclusions,
        formal_run_id=formal_run_id,
        rtl=rtl,
        spec=spec,
        dut=dut,
        output=output,
        output_dir=output_dir,
        provider_name=provider_name,
        intent=intent,
        filelists=filelists,
        include_dirs=include_dirs,
        defines=defines,
        top_module=top_module,
        work_library=work_library,
        adapter_args=adapter_args,
    )


def gen_sva(
    root: Path | None,
    spec: Path,
    rtl: Path,
    output: Path | None = None,
    provider_name: str | None = None,
    *,
    filelists: list[str] | None = None,
    include_dirs: list[str] | None = None,
    defines: list[str] | None = None,
    top_module: str | None = None,
    work_library: str | None = None,
    adapter_args: list[str] | None = None,
) -> dict[str, object]:
    config, store, retrieval = load_services(root)
    spec_path = spec if spec.is_absolute() else (config.project_root / spec).resolve()
    rtl_path = rtl if rtl.is_absolute() else (config.project_root / rtl).resolve()
    _require_file(spec_path, f"spec file does not exist: {relative_to_project(config, spec_path)}")
    _require_file(rtl_path, f"rtl file does not exist: {relative_to_project(config, rtl_path)}")
    output_path = None if output is None else (output if output.is_absolute() else (config.project_root / output).resolve())
    provider = build_generation_provider(config, provider_name=provider_name)
    run_spec = _adapter_run_spec(
        files=[relative_to_project(config, rtl_path)],
        filelists=filelists,
        include_dirs=include_dirs,
        defines=defines,
        top_module=top_module,
        work_library=work_library,
        extra_args=adapter_args,
    )
    candidate, validation_run, context = execute_generation(config, store, retrieval, provider, spec_path, rtl_path, output_path=output_path, run_spec=run_spec)
    validation_mode = _validation_mode(validation_run)
    workflow_status = _sva_workflow_status(candidate.status if candidate else "no_generation", validation_run)
    return {
        "context_id": context.context_id,
        "candidate_id": candidate.candidate_id if candidate else None,
        "provider": candidate.provider if candidate else getattr(provider, "name", ""),
        "status": workflow_status,
        "workflow_status": workflow_status,
        "initial_tool_status": None,
        "candidate_status": candidate.status if candidate else "no_generation",
        "review_status": "pending_review" if candidate and candidate.status == "validated" else "not_available",
        "artifact_path": candidate.file_path if candidate else None,
        "spec_path": candidate.spec_path if candidate else str(spec_path.relative_to(config.project_root)).replace("\\", "/"),
        "rtl_path": candidate.rtl_path if candidate else str(rtl_path.relative_to(config.project_root)).replace("\\", "/"),
        "explanation": candidate.explanation if candidate else None,
        "property_summaries": [dataclass_to_dict(item) for item in candidate.properties] if candidate else [],
        "evidence_paths": candidate.evidence_paths if candidate else [],
        "replay_artifacts": candidate.replay_artifacts if candidate else {},
        "attempts": candidate.attempts if candidate else [],
        "rejected_candidate_ids": candidate.rejected_candidate_ids if candidate else [],
        "validation_run_id": validation_run.run_id if validation_run else None,
        "validation_status": validation_run.status if validation_run else None,
        "validation_summary": validation_run.summary if validation_run else None,
        "validation_mode": validation_mode,
        "validation_scope": validation_mode,
        "structural_status": validation_run.tool_result.get("structural_status") if validation_run else None,
        "syntax_status": validation_run.tool_result.get("syntax_status") if validation_run else None,
        "adapter_status": validation_run.tool_result.get("adapter_status") if validation_run else None,
        "validation_limitations": validation_run.tool_result.get("limitations", []) if validation_run else [],
        "validation_stages": validation_run.tool_result.get("stages", {}) if validation_run else {},
        "formal_status": validation_run.tool_result.get("formal_status") if validation_run else None,
        "proof_status": validation_run.tool_result.get("proof_status") if validation_run else None,
        "overall_status": validation_run.tool_result.get("overall_status") if validation_run else None,
        "formal_adapter": validation_run.tool_result.get("formal_adapter") if validation_run else None,
        "command_artifacts": validation_run.tool_result.get("command_artifacts", {}) if validation_run else {},
        "setup_diagnostics": validation_run.tool_result.get("setup_diagnostics", []) if validation_run else [],
    }


def gen_cocotb(
    root: Path | None,
    dut: Path,
    spec: Path | None = None,
    output_dir: Path | None = None,
    intent: str = "",
    provider_name: str | None = None,
    *,
    filelists: list[str] | None = None,
    include_dirs: list[str] | None = None,
    defines: list[str] | None = None,
    top_module: str | None = None,
    work_library: str | None = None,
    adapter_args: list[str] | None = None,
) -> dict[str, object]:
    config, store, retrieval = load_services(root)
    dut_path = dut if dut.is_absolute() else (config.project_root / dut).resolve()
    spec_path = None if spec is None else (spec if spec.is_absolute() else (config.project_root / spec).resolve())
    _require_file(dut_path, f"dut file does not exist: {relative_to_project(config, dut_path)}")
    if spec_path is not None:
        _require_file(spec_path, f"spec file does not exist: {relative_to_project(config, spec_path)}")
    target_output_dir = None if output_dir is None else (output_dir if output_dir.is_absolute() else (config.project_root / output_dir).resolve())
    provider = build_generation_provider(config, provider_name=provider_name)
    run_spec = _adapter_run_spec(
        files=[relative_to_project(config, dut_path)],
        filelists=filelists,
        include_dirs=include_dirs,
        defines=defines,
        top_module=top_module,
        work_library=work_library,
        extra_args=adapter_args,
    )
    candidate, run, validation_run, context = execute_cocotb_generation(
        config,
        store,
        retrieval,
        provider,
        dut_path,
        spec_path=spec_path,
        output_dir=target_output_dir,
        intent=intent,
        run_spec=run_spec,
    )
    validation_mode = _validation_mode(validation_run)
    return {
        "context_id": context.context_id,
        "run_id": run.run_id if run else None,
        "candidate_id": candidate.candidate_id if candidate else None,
        "provider": candidate.provider if candidate else getattr(provider, "name", ""),
        "status": candidate.status if candidate else "no_generation",
        "workflow_status": candidate.status if candidate else "no_generation",
        "initial_tool_status": None,
        "candidate_status": candidate.status if candidate else "no_generation",
        "review_status": "pending_review" if candidate and candidate.status == "validated" else "not_available",
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
        "attempts": candidate.attempts if candidate else [],
        "rejected_candidate_ids": candidate.rejected_candidate_ids if candidate else [],
        "validation_run_id": validation_run.run_id if validation_run else None,
        "validation_status": validation_run.status if validation_run else None,
        "validation_summary": validation_run.summary if validation_run else None,
        "validation_mode": validation_mode,
        "validation_scope": validation_mode,
        "validation_limitations": validation_run.tool_result.get("limitations", []) if validation_run else [],
        "executable_status": validation_run.tool_result.get("executable_status") if validation_run else None,
        "executable_contract": validation_run.tool_result.get("executable_contract") if validation_run else None,
        "simulator": validation_run.tool_result.get("simulator") if validation_run else None,
        "validation_stages": validation_run.tool_result.get("stages", {}) if validation_run else {},
        "command_artifacts": validation_run.tool_result.get("command_artifacts", {}) if validation_run else {},
        "setup_diagnostics": validation_run.tool_result.get("setup_diagnostics", []) if validation_run else [],
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
    _require_file(report_path, f"coverage report does not exist: {relative_to_project(config, report_path)}")
    if exclusions_path is not None:
        _require_file(exclusions_path, f"coverage exclusions file does not exist: {relative_to_project(config, exclusions_path)}")
    for path in rtl_paths:
        _require_file(path, f"rtl file does not exist: {relative_to_project(config, path)}")
    for path in spec_paths:
        _require_file(path, f"spec file does not exist: {relative_to_project(config, path)}")
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
    for path in logs:
        resolved = path if path.is_absolute() else (config.project_root / path).resolve()
        _require_existing_path(resolved, f"log path does not exist: {relative_to_project(config, resolved)}")
    for path in waveforms or []:
        resolved = path if path.is_absolute() else (config.project_root / path).resolve()
        _require_file(resolved, f"waveform file does not exist: {relative_to_project(config, resolved)}")
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
    provider_configs = config.project.model_policy.get("providers", {})
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
                "model": provider_configs.get(status.name, {}).get("model") if isinstance(provider_configs.get(status.name), dict) else None,
                "base_provider": provider_configs.get(status.name, {}).get("base_provider") if isinstance(provider_configs.get(status.name), dict) else None,
                "runtime": provider_configs.get(status.name, {}).get("runtime") if isinstance(provider_configs.get(status.name), dict) else None,
                "timeout_seconds": provider_configs.get(status.name, {}).get("timeout_seconds") if isinstance(provider_configs.get(status.name), dict) else None,
                "network_scope": status.network_scope,
                "auth_mode": status.auth_mode,
                **_provider_model_details(status.name, provider_configs),
            }
            for status in list_provider_statuses(config)
        ],
    }


def coverage_import(
    root: Path | None,
    source: Path,
    *,
    source_format: str,
    output: Path,
) -> dict[str, object]:
    config, _, _ = load_services(root)
    source_path = source if source.is_absolute() else (config.project_root / source).resolve()
    _require_file(source_path, f"coverage source does not exist: {relative_to_project(config, source_path)}")
    return import_coverage_report(config, source_path, source_format=source_format, output=output)


def check_providers(root: Path | None = None, provider_name: str | None = None, *, live: bool = True) -> dict[str, object]:
    config, _, _ = load_services(root)
    checks = [dataclass_to_dict(check) for check in check_provider_statuses(config, provider_name, live=live)]
    return {
        "live": live,
        "providers": checks,
        "status": "passed" if all(item["status"] == "passed" for item in checks) else "failed",
    }


def list_model_options(root: Path | None = None, *, live: bool = True) -> dict[str, object]:
    config, _, _ = load_services(root)
    return list_model_options_catalog(config, live=live)


def select_model_provider(root: Path | None, capability: str, provider_name: str) -> dict[str, object]:
    config, _, _ = load_services(root)
    return set_default_provider_catalog(config, capability, provider_name)


def set_provider_model(root: Path | None, provider_name: str, model: str) -> dict[str, object]:
    config, _, _ = load_services(root)
    return set_provider_model_catalog(config, provider_name, model)


def set_provider_reasoning(root: Path | None, provider_name: str, level: str) -> dict[str, object]:
    config, _, _ = load_services(root)
    return set_provider_reasoning_catalog(config, provider_name, level)


def setup_provider(
    root: Path | None,
    provider_name: str,
    *,
    kind: str,
    capabilities: list[str] | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    auth: str | None = None,
    timeout_seconds: int | None = None,
    select_defaults: bool = False,
) -> dict[str, object]:
    config, _, _ = load_services(root)
    provider_name = provider_name.strip()
    if not provider_name:
        raise ConfigError("provider name must be non-empty")
    normalized_kind = kind.strip().lower().replace("_", "-")
    if normalized_kind not in {"openai-compatible", "anthropic", "local-openai"}:
        raise ConfigError("provider setup kind must be openai-compatible, anthropic, or local-openai")
    selected_capabilities = _normalize_provider_setup_capabilities(capabilities)
    provider_config = _provider_setup_config(
        normalized_kind,
        selected_capabilities,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        auth=auth,
        timeout_seconds=timeout_seconds,
    )
    policy = config.project.model_policy
    providers = policy.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise ConfigError("project.model_policy.providers must be an object")
    providers[provider_name] = provider_config
    selected_defaults: dict[str, str] = {}
    if select_defaults:
        defaults = dict(config.default_provider_by_capability())
        for capability in selected_capabilities:
            defaults[capability] = provider_name
            selected_defaults[capability] = provider_name
        policy["default_provider_by_capability"] = defaults
    config.save()
    next_steps = []
    if provider_config.get("auth") != "none" and provider_config.get("api_key_env"):
        next_steps.append(f"Set {provider_config['api_key_env']} in your shell or ignored .env file.")
    model_env_hint = _provider_setup_model_env_hint(normalized_kind, provider_config)
    if model_env_hint:
        next_steps.append(f"Override the default model with `{model_env_hint}` or `tel providers set-model {provider_name} MODEL` when needed.")
    next_steps.append(f"Run `tel providers check {provider_name}`.")
    if not select_defaults:
        for capability in selected_capabilities:
            next_steps.append(f"Run `tel providers select --capability {capability} --provider {provider_name}` to make it the default.")
    return {
        "status": "updated",
        "provider": provider_name,
        "kind": provider_config["kind"],
        "setup_kind": normalized_kind,
        "capabilities": selected_capabilities,
        "stored_config_keys": sorted(provider_config.keys()),
        "selected_defaults": selected_defaults,
        "default_provider_by_capability": config.default_provider_by_capability(),
        "next_steps": next_steps,
    }


def _normalize_provider_setup_capabilities(capabilities: list[str] | None) -> list[str]:
    raw = capabilities or ["repair", "generation"]
    normalized: list[str] = []
    for capability in raw:
        value = capability.strip()
        if value not in {"repair", "generation"}:
            raise ConfigError("capability must be repair or generation")
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise ConfigError("at least one capability is required")
    return normalized


def _provider_setup_config(
    setup_kind: str,
    capabilities: list[str],
    *,
    model: str | None,
    base_url: str | None,
    api_key_env: str | None,
    auth: str | None,
    timeout_seconds: int | None,
) -> dict[str, object]:
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ConfigError("timeout_seconds must be a positive integer")
    normalized_api_key_env = _normalize_api_key_env(api_key_env)
    if setup_kind == "anthropic":
        resolved_base_url = (base_url or "https://api.anthropic.com/v1").strip()
        if not normalized_api_key_env:
            raise ConfigError("api_key_env is required for anthropic provider setup")
        resolved_model = _resolve_provider_setup_model(setup_kind, model, resolved_base_url)
        return {
            "kind": "anthropic",
            "capabilities": capabilities,
            "base_url": resolved_base_url,
            "model": resolved_model,
            "api_key_env": normalized_api_key_env,
            "timeout_seconds": timeout_seconds or 90,
        }
    resolved_auth = (auth or ("none" if setup_kind == "local-openai" else "bearer")).strip()
    if resolved_auth not in {"bearer", "none"}:
        raise ConfigError("auth must be bearer or none")
    if setup_kind == "openai-compatible":
        resolved_base_url = (base_url or "").strip()
        if not resolved_base_url:
            raise ConfigError("base_url is required for openai-compatible provider setup")
    else:
        resolved_base_url = (base_url or "http://127.0.0.1:11434/v1").strip()
    resolved_model = _resolve_provider_setup_model(setup_kind, model, resolved_base_url)
    provider_config: dict[str, object] = {
        "kind": "openai_compatible",
        "capabilities": capabilities,
        "base_url": resolved_base_url,
        "model": resolved_model,
        "auth": resolved_auth,
        "timeout_seconds": timeout_seconds or 60,
    }
    if resolved_auth != "none":
        if not normalized_api_key_env:
            raise ConfigError("api_key_env is required unless auth is none")
        provider_config["api_key_env"] = normalized_api_key_env
    elif normalized_api_key_env:
        provider_config["api_key_env"] = normalized_api_key_env
    return provider_config


def _resolve_provider_setup_model(setup_kind: str, model: str | None, base_url: str) -> str:
    value = (model or "").strip()
    if value:
        return value
    if setup_kind == "anthropic":
        return DEFAULT_ANTHROPIC_MODEL
    if setup_kind == "openai-compatible" and _is_official_openai_base_url(base_url):
        return DEFAULT_OPENAI_API_MODEL
    if setup_kind == "openai-compatible":
        raise ConfigError(
            "model is required for openai-compatible provider setup unless base_url is https://api.openai.com/v1; "
            f"for official OpenAI, suggested default is {DEFAULT_OPENAI_API_MODEL}"
        )
    raise ConfigError("model is required for local-openai provider setup")


def _is_official_openai_base_url(base_url: str) -> bool:
    parsed = urlparse(base_url)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "api.openai.com"


def _provider_setup_model_env_hint(setup_kind: str, provider_config: dict[str, object]) -> str:
    if setup_kind == "anthropic":
        return "TELCHINES_ANTHROPIC_MODEL"
    if setup_kind == "openai-compatible" and _is_official_openai_base_url(str(provider_config.get("base_url") or "")):
        return "TELCHINES_OPENAI_MODEL"
    return ""


def _normalize_api_key_env(api_key_env: str | None) -> str:
    value = (api_key_env or "").strip()
    if not value:
        return ""
    if not ENV_VAR_NAME_RE.fullmatch(value):
        raise ConfigError("api_key_env must be an uppercase environment variable name like OPENROUTER_API_KEY, not a literal secret value")
    return value


def _provider_model_details(provider_name: str, provider_configs: dict[str, object]) -> dict[str, object]:
    provider_config = provider_configs.get(provider_name)
    if not isinstance(provider_config, dict):
        return {}
    return provider_model_metadata(provider_name, provider_config, provider_configs)


def run_eval(root: Path | None = None) -> dict[str, object]:
    start = root or Path.cwd()
    try:
        config, store, _ = load_services(start)
    except ProjectNotInitializedError:
        return _run_eval_in_scratch(start)
    report = run_default_suite(config, store)
    return {**report, "project_context": "project", "report_persisted": True}


def load_eval_report(root: Path | None = None) -> dict[str, object]:
    try:
        _, store, _ = load_services(root)
    except ProjectNotInitializedError as exc:
        raise ConfigError("eval reports are persisted only for initialized Telchines projects; run `tel project init` or run `tel eval run` for scratch output") from exc
    return store.load_report("latest_eval")


def _run_eval_in_scratch(start: Path) -> dict[str, object]:
    scratch_root = Path(tempfile.mkdtemp(prefix="telchines-eval-scratch-"))
    try:
        local_benchmarks_root = start.resolve() / "benchmarks"
        if local_benchmarks_root.exists() and any(local_benchmarks_root.glob("*.json")):
            shutil.copytree(local_benchmarks_root, scratch_root / "benchmarks")
            benchmark_source = str(local_benchmarks_root)
        else:
            benchmark_source = "bundled"
        config = ProjectConfig.init_project(scratch_root, name="telchines-eval-scratch")
        report = run_default_suite(config, RunStore(config))
        return {
            **report,
            "project_context": "scratch",
            "report_persisted": False,
            "scratch_project": str(scratch_root),
            "benchmark_source": benchmark_source,
        }
    finally:
        remove_tree(scratch_root)


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


def inspect_waveform(
    root: Path | None,
    target: str,
    signal: str,
    window: int = 8,
    *,
    start_time: int | None = None,
    end_time: int | None = None,
    log_path: str | None = None,
    tolerance_ticks: int = 0,
) -> dict[str, object]:
    config, store, _ = load_services(root)
    summary = _resolve_waveform_summary(config, store, root or config.project_root, target)
    sample, match_type = match_signal(summary, signal)
    window_summary = summarize_signal_window(summary, sample, start_time=start_time, end_time=end_time)
    transitions = [
        transition
        for transition in sample.transitions
        if (start_time is None or transition.timestamp >= start_time) and (end_time is None or transition.timestamp <= end_time)
    ][: max(window, 1)]
    correlations: list[dict[str, object]] = []
    if log_path:
        candidate = Path(log_path)
        resolved_log = candidate.resolve() if candidate.is_absolute() else (config.project_root / candidate).resolve()
        try:
            resolved_log.relative_to(config.project_root.resolve())
        except ValueError as exc:
            raise ValueError("waveform correlation log must be inside the project root") from exc
        if not resolved_log.is_file():
            raise ValueError(f"waveform correlation log does not exist: {resolved_log}")
        correlations = correlate_log_timestamps(summary, resolved_log.read_text(encoding="utf-8", errors="replace"), tolerance_ticks=tolerance_ticks)
    return {
        "waveform_id": summary.waveform_id,
        "source_path": summary.source_path,
        "signal_name": sample.signal_name,
        "full_name": sample.full_name,
        "match_type": match_type,
        "timescale": summary.timescale,
        "transition_count": len(sample.transitions),
        "transitions": [dataclass_to_dict(item) for item in transitions],
        "window_summary": window_summary,
        "log_correlations": correlations,
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
                "log_family": cluster.get("log_family", ""),
                "tool_name": cluster.get("tool_name", ""),
                "domain": cluster.get("domain", ""),
                "likely_cause": cluster["likely_cause"],
                "suggested_action": cluster["suggested_action"],
                "evidence": [hit["citation"] for hit in cluster["evidence_hits"]],
                "waveforms": [
                    {
                        "waveform_id": item["waveform_id"],
                        "source_path": item["source_path"],
                        "matched_signals": item["matched_signals"],
                        "relevance": item.get("relevance", "unrelated"),
                        "evidence_status": item.get("evidence_status", item.get("relevance", "unrelated")),
                        "score": item.get("score", 0.0),
                        "reason": item.get("reason", ""),
                        "candidate_signals": item.get("candidate_signals", []),
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
        waveforms = ", ".join(_format_waveform_evidence(item) for item in cluster["waveform_evidence"][:2]) or "none"
        domain = _format_cluster_domain(cluster)
        formal = ", ".join(
            f"{item['run_id']} [{item['status']}]"
            + (f" props={', '.join(item['property_ids'][:2])}" if item.get("property_ids") else "")
            for item in cluster.get("formal_evidence", [])[:2]
        ) or "none"
        lines.extend(
            [
                "",
                f"{index}. {cluster['summary']}",
                f"domain: {domain}",
                f"likely cause: {cluster['likely_cause']}",
                f"suggested action: {cluster['suggested_action']}",
                f"evidence: {evidence}",
                f"formal: {formal}",
                f"waveforms: {waveforms}",
                f"similar runs: {similar}",
            ]
        )
    return "\n".join(lines)


def _format_waveform_evidence(item: dict[str, object]) -> str:
    relevance = str(item.get("relevance", "unrelated"))
    evidence_status = str(item.get("evidence_status", relevance))
    reason = str(item.get("reason", "")).strip()
    signals = ", ".join(str(signal) for signal in item.get("matched_signals", []))
    if signals:
        detail = f"{signals}; {evidence_status}"
    else:
        detail = evidence_status
    if reason:
        detail = f"{detail}; {reason}"
    return f"{item['source_path']} ({detail})"


def _format_cluster_domain(cluster: dict[str, object]) -> str:
    parts = [
        str(cluster.get("domain", "")).strip(),
        str(cluster.get("log_family", "")).strip(),
        str(cluster.get("tool_name", "")).strip(),
    ]
    return " / ".join(part for part in parts if part) or "generic"


def dump_json(value: object) -> str:
    return json.dumps(value, indent=2)


def relative_to_project(config: ProjectConfig, path: Path) -> str:
    try:
        return path.resolve().relative_to(config.project_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _require_file(path: Path, message: str) -> None:
    if not path.exists() or not path.is_file():
        raise WorkflowInputError(message)


def _require_existing_path(path: Path, message: str) -> None:
    if not path.exists():
        raise WorkflowInputError(message)


def _validate_agent_inputs(
    config: ProjectConfig,
    *,
    logs: list[Path] | None,
    waveforms: list[Path] | None,
    report: Path | None,
    exclusions: Path | None,
    rtl: list[Path] | None,
    spec: list[Path] | None,
    dut: Path | None,
) -> None:
    for path in logs or []:
        resolved = path if path.is_absolute() else (config.project_root / path).resolve()
        _require_existing_path(resolved, f"log path does not exist: {relative_to_project(config, resolved)}")
    for path in waveforms or []:
        resolved = path if path.is_absolute() else (config.project_root / path).resolve()
        _require_file(resolved, f"waveform file does not exist: {relative_to_project(config, resolved)}")
    if report is not None:
        resolved = report if report.is_absolute() else (config.project_root / report).resolve()
        _require_file(resolved, f"coverage report does not exist: {relative_to_project(config, resolved)}")
    if exclusions is not None:
        resolved = exclusions if exclusions.is_absolute() else (config.project_root / exclusions).resolve()
        _require_file(resolved, f"coverage exclusions file does not exist: {relative_to_project(config, resolved)}")
    for path in rtl or []:
        resolved = path if path.is_absolute() else (config.project_root / path).resolve()
        _require_file(resolved, f"rtl file does not exist: {relative_to_project(config, resolved)}")
    for path in spec or []:
        resolved = path if path.is_absolute() else (config.project_root / path).resolve()
        _require_file(resolved, f"spec file does not exist: {relative_to_project(config, resolved)}")
    if dut is not None:
        resolved = dut if dut.is_absolute() else (config.project_root / dut).resolve()
        _require_file(resolved, f"dut file does not exist: {relative_to_project(config, resolved)}")


def _validation_mode(validation_run: VerificationRun | None) -> str | None:
    if validation_run is None:
        return None
    mode = str(validation_run.tool_result.get("validation_mode", "")).strip()
    return mode or None


def _sva_workflow_status(candidate_status: str, validation_run: VerificationRun | None) -> str:
    if candidate_status != "validated" or validation_run is None:
        return candidate_status
    if validation_run.tool_result.get("overall_status") == "passed_with_warnings":
        return "validated_with_warnings"
    return "validated"


def _replayability(run: VerificationRun) -> dict[str, str]:
    stored = run.tool_result.get("replayability")
    if isinstance(stored, dict) and isinstance(stored.get("status"), str) and isinstance(stored.get("reason"), str):
        return {"status": stored["status"], "reason": stored["reason"]}
    return _replayability_from_command(run.replay_command, imported=run.workflow_type == "regression_import")


def _replayability_from_command(command: object, *, imported: bool) -> dict[str, str]:
    if isinstance(command, list) and command:
        return {"status": "replayable", "reason": "stored replay command is available"}
    if imported:
        return {"status": "not_replayable", "reason": "imported run did not include a replay command"}
    return {"status": "not_recorded", "reason": "workflow did not record a replay command"}


def _adapter_run_spec(
    *,
    files: list[str] | None = None,
    filelists: list[str] | None = None,
    include_dirs: list[str] | None = None,
    defines: list[str] | None = None,
    top_module: str | None = None,
    work_library: str | None = None,
    extra_args: list[str] | None = None,
    timeout_seconds: int | None = None,
) -> AdapterRunSpec:
    return AdapterRunSpec(
        files=[str(item) for item in files or []],
        filelists=[str(item) for item in filelists or []],
        include_dirs=[str(item) for item in include_dirs or []],
        defines=[str(item) for item in defines or []],
        top_module=top_module,
        work_library=work_library,
        timeout_seconds=timeout_seconds,
        extra_args=[str(item) for item in extra_args or []],
    )


def _repair_workflow_status(candidate_status: str, validation_status: str | None, *, apply_patch: bool) -> str:
    if candidate_status == "no_patch":
        return "no_patch"
    if validation_status == "passed":
        return "applied" if apply_patch else "review_required"
    if validation_status == "failed" or candidate_status == "rejected":
        return "rejected"
    return candidate_status or "failed"


def _repair_review_status(workflow_status: str) -> str:
    if workflow_status == "review_required":
        return "pending_review"
    if workflow_status == "applied":
        return "applied"
    return "not_available"


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
