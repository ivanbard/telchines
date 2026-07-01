from __future__ import annotations

from telchines import repair_validation
from telchines.adapters.registry import AdapterRegistry
from telchines.config import ProjectConfig
from telchines.models import AgentTask, PatchProposal, RetrievalContext, ValidationAttempt, VerificationRun
from telchines.providers import RepairProvider, RepairRequest
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import stable_id, utc_now


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

    agent_runtime = provider_result.response_payload.get("agent_runtime", {})
    if isinstance(agent_runtime, dict):
        proposal.runtime_mode = str(agent_runtime.get("runtime_mode") or "")
        runtime_available = agent_runtime.get("runtime_available")
        proposal.runtime_available = runtime_available if isinstance(runtime_available, bool) else None
        proposal.runtime_reason = str(agent_runtime.get("runtime_reason") or "")
        task.metadata["runtime_mode"] = proposal.runtime_mode
        task.metadata["runtime_available"] = proposal.runtime_available
        task.metadata["runtime_reason"] = proposal.runtime_reason

    proposal.replay_artifacts = {
        "request_artifact": str(request_artifact),
        "response_artifact": str(response_artifact),
        "replay_artifact": str(replay_artifact),
    }
    validation_run = _provider_validation_run(config, store, provider_result)
    if validation_run is None:
        validation_run = validate_patch(config, store, base_run, proposal, apply_patch=apply_patch)
    elif apply_patch and validation_run.status == "passed":
        (config.project_root / proposal.file_path).write_text(proposal.candidate_content, encoding="utf-8")
    proposal.status = "validated" if validation_run.status == "passed" else "rejected"
    if not proposal.validation_attempts:
        proposal.validation_attempts.append(
            ValidationAttempt(attempt=1, result=validation_run.status, run_id=validation_run.run_id, notes=validation_run.summary)
        )
    task.metadata["patch_id"] = proposal.patch_id
    task.metadata["validation_run_id"] = validation_run.run_id
    task.status = proposal.status
    store.save_patch(proposal)
    store.save_task(task)
    return proposal, validation_run, context


def _provider_validation_run(config: ProjectConfig, store: RunStore, provider_result) -> VerificationRun | None:
    agent_runtime = provider_result.response_payload.get("agent_runtime", {})
    if not isinstance(agent_runtime, dict):
        return None
    validation_run_id = agent_runtime.get("validation_run_id")
    if not isinstance(validation_run_id, str) or not validation_run_id:
        return None
    return store.load_run(validation_run_id)


def validate_patch(config: ProjectConfig, store: RunStore, base_run: VerificationRun, proposal: PatchProposal, apply_patch: bool = False) -> VerificationRun:
    original_registry = repair_validation.AdapterRegistry
    repair_validation.AdapterRegistry = AdapterRegistry
    try:
        return repair_validation.validate_patch(config, store, base_run, proposal, apply_patch=apply_patch)
    finally:
        repair_validation.AdapterRegistry = original_registry
