from __future__ import annotations

import subprocess

from telchines.adapters.registry import AdapterRegistry
from telchines.adapters.parsing import parse_common_output
from telchines.config import ProjectConfig
from telchines.models import AgentTask, PatchProposal, RetrievalContext, ValidationAttempt, VerificationRun
from telchines.providers import RepairProvider, RepairRequest
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import copy_tree_to_temp, ensure_directory, remove_tree, stable_id, utc_now


def execute_repair(
    config: ProjectConfig,
    store: RunStore,
    retrieval: RetrievalService,
    provider: RepairProvider,
    base_run: VerificationRun,
    apply_patch: bool = False,
) -> tuple[PatchProposal | None, VerificationRun | None, RetrievalContext]:
    observations = store.load_observations(base_run.observation_ids)
    query = " ".join(filter(None, [observation.message for observation in observations] + [base_run.summary]))
    context = retrieval.search(query=query, limit=int(config.retrieval.get("max_hits", 5)), mode="repair")
    store.save_context(context)

    task = AgentTask(
        task_id=stable_id("task", config.project.project_id, base_run.run_id, utc_now()),
        project_id=config.project.project_id,
        workflow_type="compile_repair",
        input_run_id=base_run.run_id,
        status="running",
        created_at=utc_now(),
        metadata={},
    )
    store.save_task(task)

    request = RepairRequest(
        task_id=task.task_id,
        project_root=config.project_root,
        base_run=base_run,
        observations=observations,
        retrieval_context=context,
    )
    provider_result = provider.propose_patch(request)
    request_artifact = store.save_task_artifact(task.task_id, "repair_request", provider_result.request_payload)
    response_artifact = store.save_task_artifact(task.task_id, "repair_response", provider_result.response_payload)
    replay_artifact = store.save_task_artifact(
        task.task_id,
        "repair_replay",
        {
            "task_id": task.task_id,
            "base_run_id": base_run.run_id,
            "provider": provider_result.provider_name,
            "context_id": context.context_id,
            "observation_ids": base_run.observation_ids,
            "request_artifact": str(request_artifact),
            "response_artifact": str(response_artifact),
        },
    )
    task.metadata = {
        "provider": provider_result.provider_name,
        "context_id": context.context_id,
        "request_artifact": str(request_artifact),
        "response_artifact": str(response_artifact),
        "replay_artifact": str(replay_artifact),
        "provider_summary": provider_result.summary,
    }

    proposal = provider_result.proposal
    if proposal is None:
        task.status = "no_patch"
        store.save_task(task)
        return None, None, context

    proposal.replay_artifacts = {
        "request_artifact": str(request_artifact),
        "response_artifact": str(response_artifact),
        "replay_artifact": str(replay_artifact),
    }
    validation_run = validate_patch(config, store, base_run, proposal, apply_patch=apply_patch)
    proposal.status = "validated" if validation_run.status == "passed" else "rejected"
    proposal.validation_attempts.append(
        ValidationAttempt(attempt=1, result=validation_run.status, run_id=validation_run.run_id, notes=validation_run.summary)
    )
    task.metadata["patch_id"] = proposal.patch_id
    task.metadata["validation_run_id"] = validation_run.run_id
    task.status = proposal.status
    store.save_patch(proposal)
    store.save_task(task)
    return proposal, validation_run, context


def validate_patch(config: ProjectConfig, store: RunStore, base_run: VerificationRun, proposal: PatchProposal, apply_patch: bool = False) -> VerificationRun:
    temp_root = copy_tree_to_temp(config.project_root)
    try:
        target = temp_root / proposal.file_path
        ensure_directory(target.parent)
        target.write_text(proposal.candidate_content, encoding="utf-8")
        run_id = stable_id("run", base_run.run_id, "validation", utc_now())
        artifacts_dir = config.project_root / config.artifacts_dir
        ensure_directory(artifacts_dir)
        execution = _run_validation_execution(base_run, temp_root, artifacts_dir, run_id)
        observations = execution["observations"]
        store.save_observations(observations)
        validation_run = VerificationRun(
            run_id=run_id,
            project_id=config.project.project_id,
            commit_sha=base_run.commit_sha,
            workflow_type="repair_validation",
            tool=base_run.tool,
            inputs=base_run.inputs,
            status="passed" if execution["exit_code"] == 0 else "failed",
            started_at=utc_now(),
            finished_at=utc_now(),
            exit_code=execution["exit_code"],
            artifacts=execution["artifacts"],
            tool_result=execution["tool_result"],
            observation_ids=[observation.observation_id for observation in observations],
            summary=_validation_summary(execution["exit_code"], observations, execution["tool_result"]),
            replay_command=base_run.replay_command,
        )
        if apply_patch and execution["exit_code"] == 0:
            real_target = config.project_root / proposal.file_path
            real_target.write_text(proposal.candidate_content, encoding="utf-8")
        store.save_run(validation_run)
        return validation_run
    finally:
        remove_tree(temp_root)


def _run_validation_execution(base_run: VerificationRun, temp_root, artifacts_dir, run_id: str) -> dict[str, object]:
    adapter = _resolve_validation_adapter(base_run)
    if adapter is not None:
        files = list(base_run.inputs.get("files", []))
        extra_args = list(base_run.inputs.get("extra_args", []))
        execution = adapter.run(run_id, temp_root, files, artifacts_dir, extra_args=extra_args)
        store_like = {
            "exit_code": execution.exit_code,
            "artifacts": execution.artifacts,
            "tool_result": execution.result,
            "observations": execution.observations,
        }
        return store_like

    result = subprocess.run(base_run.replay_command, cwd=temp_root, capture_output=True, text=True, check=False)
    log_path = artifacts_dir / f"{run_id}.log"
    combined = result.stdout + result.stderr
    log_path.write_text(combined, encoding="utf-8")
    observations = parse_common_output(run_id, combined)
    return {
        "exit_code": result.returncode,
        "artifacts": {"log_path": str(log_path)},
        "tool_result": {"status": "passed" if result.returncode == 0 else "failed", "validation_mode": "legacy_replay"},
        "observations": observations,
    }


def _resolve_validation_adapter(base_run: VerificationRun):
    try:
        adapter = AdapterRegistry().get(base_run.tool.name)
    except KeyError:
        return None
    if "repair_validation" not in adapter.supported_workflows:
        return None
    return adapter


def _validation_summary(exit_code: int, observations: list[object], tool_result: dict[str, object] | None = None) -> str:
    if exit_code == 0:
        mode = str((tool_result or {}).get("validation_mode", "")).strip()
        if mode:
            return f"validation passed via {mode}"
        return "validation command passed"
    if observations:
        first = observations[0]
        return f"validation failed with {len(observations)} observation(s); first: {first.signature}"
    if tool_result:
        status = str(tool_result.get("status", "")).strip()
        if status:
            return f"validation failed with status {status}"
    return f"validation failed with exit code {exit_code}"
