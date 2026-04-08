from __future__ import annotations

import subprocess

from ovai.adapters.parsing import parse_common_output
from ovai.config import ProjectConfig
from ovai.models import AgentTask, PatchProposal, RetrievalContext, ValidationAttempt, VerificationRun
from ovai.providers import RepairProvider
from ovai.retrieval import RetrievalService
from ovai.run_store import RunStore
from ovai.utils import copy_tree_to_temp, ensure_directory, remove_tree, stable_id, utc_now


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
    context = retrieval.search(query=query, limit=int(config.retrieval.get("max_hits", 5)))
    store.save_context(context)

    task = AgentTask(
        task_id=stable_id("task", config.project.project_id, base_run.run_id, utc_now()),
        project_id=config.project.project_id,
        workflow_type="compile_repair",
        input_run_id=base_run.run_id,
        status="running",
        created_at=utc_now(),
    )
    store.save_task(task)

    proposal = provider.propose_patch(task.task_id, config.project_root, observations)
    if proposal is None:
        task.status = "no_patch"
        store.save_task(task)
        return None, None, context

    validation_run = validate_patch(config, store, base_run, proposal, apply_patch=apply_patch)
    proposal.status = "validated" if validation_run.status == "passed" else "rejected"
    proposal.validation_attempts.append(
        ValidationAttempt(attempt=1, result=validation_run.status, run_id=validation_run.run_id, notes=validation_run.summary)
    )
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
        result = subprocess.run(base_run.replay_command, cwd=temp_root, capture_output=True, text=True, check=False)
        run_id = stable_id("run", base_run.run_id, "validation", utc_now())
        artifacts_dir = config.project_root / config.artifacts_dir
        ensure_directory(artifacts_dir)
        log_path = artifacts_dir / f"{run_id}.log"
        combined = result.stdout + result.stderr
        log_path.write_text(combined, encoding="utf-8")
        observations = parse_common_output(run_id, combined)
        store.save_observations(observations)
        validation_run = VerificationRun(
            run_id=run_id,
            project_id=config.project.project_id,
            commit_sha=base_run.commit_sha,
            workflow_type="repair_validation",
            tool=base_run.tool,
            inputs=base_run.inputs,
            status="passed" if result.returncode == 0 else "failed",
            started_at=utc_now(),
            finished_at=utc_now(),
            artifacts={"log_path": str(log_path)},
            observation_ids=[observation.observation_id for observation in observations],
            summary="validation command passed" if result.returncode == 0 else "validation command still failed",
            replay_command=base_run.replay_command,
        )
        if apply_patch and result.returncode == 0:
            real_target = config.project_root / proposal.file_path
            real_target.write_text(proposal.candidate_content, encoding="utf-8")
        store.save_run(validation_run)
        return validation_run
    finally:
        remove_tree(temp_root)
