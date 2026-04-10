from __future__ import annotations

import re
from pathlib import Path

from telchines.adapters.parsing import parse_common_output
from telchines.config import ProjectConfig
from telchines.models import AgentTask, RetrievalContext, SvaCandidate, ToolReference, ValidationAttempt, VerificationRun
from telchines.providers import GenerationProvider, GenerationRequest
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import copy_tree_to_temp, ensure_directory, relative_to, remove_tree, stable_id, utc_now


def execute_generation(
    config: ProjectConfig,
    store: RunStore,
    retrieval: RetrievalService,
    provider: GenerationProvider,
    spec_path: Path,
    rtl_path: Path,
    output_path: Path | None = None,
) -> tuple[SvaCandidate | None, VerificationRun | None, RetrievalContext]:
    spec_rel = relative_to(spec_path, config.project_root)
    rtl_rel = relative_to(rtl_path, config.project_root)
    output_rel = relative_to(output_path, config.project_root) if output_path else str(Path(config.artifacts_dir) / "generated" / f"{rtl_path.stem}_assertions.sv")
    query = f"{spec_path.stem} {rtl_path.stem} assertion property SVA"
    context = retrieval.search(query=query, mode="generation", focus_paths=[spec_rel, rtl_rel])
    store.save_context(context)

    task = AgentTask(
        task_id=stable_id("task", config.project.project_id, "spec_to_sva", spec_rel, rtl_rel, utc_now()),
        project_id=config.project.project_id,
        workflow_type="spec_to_sva",
        input_run_id=None,
        status="running",
        created_at=utc_now(),
        metadata={"spec_path": spec_rel, "rtl_path": rtl_rel, "output_file": output_rel},
    )
    store.save_task(task)

    request = GenerationRequest(
        task_id=task.task_id,
        project_root=config.project_root,
        spec_path=spec_rel,
        rtl_path=rtl_rel,
        output_file=output_rel,
        retrieval_context=context,
    )
    provider_result = provider.generate_sva(request)
    request_artifact = store.save_task_artifact(task.task_id, "sva_request", provider_result.request_payload)
    response_artifact = store.save_task_artifact(task.task_id, "sva_response", provider_result.response_payload)
    replay_artifact = store.save_task_artifact(
        task.task_id,
        "sva_replay",
        {
            "task_id": task.task_id,
            "provider": provider_result.provider_name,
            "context_id": context.context_id,
            "request_artifact": str(request_artifact),
            "response_artifact": str(response_artifact),
            "spec_path": spec_rel,
            "rtl_path": rtl_rel,
            "output_file": output_rel,
        },
    )
    task.metadata.update(
        {
            "provider": provider_result.provider_name,
            "context_id": context.context_id,
            "request_artifact": str(request_artifact),
            "response_artifact": str(response_artifact),
            "replay_artifact": str(replay_artifact),
            "provider_summary": provider_result.summary,
        }
    )

    candidate = provider_result.candidate
    if candidate is None:
        task.status = "no_generation"
        store.save_task(task)
        return None, None, context

    candidate.replay_artifacts = {
        "request_artifact": str(request_artifact),
        "response_artifact": str(response_artifact),
        "replay_artifact": str(replay_artifact),
    }
    generated_path = config.project_root / candidate.file_path
    ensure_directory(generated_path.parent)
    generated_path.write_text(candidate.candidate_content, encoding="utf-8")

    validation_run = validate_sva_candidate(config, store, candidate)
    candidate.status = "validated" if validation_run.status == "passed" else "rejected"
    candidate.validation_attempts.append(
        ValidationAttempt(attempt=1, result=validation_run.status, run_id=validation_run.run_id, notes=validation_run.summary)
    )
    task.metadata["candidate_id"] = candidate.candidate_id
    task.metadata["validation_run_id"] = validation_run.run_id
    task.status = candidate.status
    store.save_sva_candidate(candidate)
    store.save_task(task)
    return candidate, validation_run, context


def validate_sva_candidate(config: ProjectConfig, store: RunStore, candidate: SvaCandidate) -> VerificationRun:
    run_id = stable_id("run", candidate.candidate_id, "validation", utc_now())
    temp_root = copy_tree_to_temp(config.project_root)
    try:
        target = temp_root / candidate.file_path
        ensure_directory(target.parent)
        target.write_text(candidate.candidate_content, encoding="utf-8")

        validator_name, command, returncode, combined = _run_validation(temp_root, candidate)
        artifacts_dir = config.project_root / config.artifacts_dir
        ensure_directory(artifacts_dir)
        log_path = artifacts_dir / f"{run_id}.log"
        log_path.write_text(combined, encoding="utf-8")
        observations = parse_common_output(run_id, combined)
        store.save_observations(observations)
        validation_run = VerificationRun(
            run_id=run_id,
            project_id=config.project.project_id,
            commit_sha="workspace",
            workflow_type="sva_validation",
            tool=ToolReference(kind="validator", name=validator_name, version="unknown"),
            inputs={"spec_path": candidate.spec_path, "rtl_path": candidate.rtl_path, "generated_file": candidate.file_path},
            status="passed" if returncode == 0 else "failed",
            started_at=utc_now(),
            finished_at=utc_now(),
            exit_code=returncode,
            artifacts={"log_path": str(log_path), "generated_file": candidate.file_path},
            observation_ids=[observation.observation_id for observation in observations],
            summary=_validation_summary(returncode, combined, validator_name),
            replay_command=command,
        )
        store.save_run(validation_run)
        return validation_run
    finally:
        remove_tree(temp_root)


def _run_validation(project_root: Path, candidate: SvaCandidate) -> tuple[str, list[str], int, str]:
    errors = _builtin_sva_errors(candidate.candidate_content)
    command = ["builtin_sva_syntax", candidate.file_path]
    if errors:
        return "builtin_sva_syntax", command, 1, "\n".join(errors)
    return "builtin_sva_syntax", command, 0, "builtin_sva_syntax: validation passed"


def _builtin_sva_errors(content: str) -> list[str]:
    errors: list[str] = []
    module_count = len(re.findall(r"(?m)^\s*module\b", content))
    endmodule_count = len(re.findall(r"(?m)^\s*endmodule\b", content))
    property_count = len(re.findall(r"(?m)^\s*property\b", content))
    endproperty_count = len(re.findall(r"(?m)^\s*endproperty\b", content))
    if module_count == 0:
        errors.append("ERROR: expected at least one module wrapper in generated SVA artifact")
    if module_count != endmodule_count:
        errors.append("ERROR: module and endmodule counts do not match")
    if property_count == 0:
        errors.append("ERROR: expected at least one property block in generated SVA artifact")
    if property_count != endproperty_count:
        errors.append("ERROR: property and endproperty counts do not match")
    if "assert property" not in content:
        errors.append("ERROR: expected at least one assert property statement")
    return errors


def _validation_summary(exit_code: int, combined: str, validator_name: str) -> str:
    if exit_code == 0:
        return f"{validator_name} validation passed"
    first_line = next((line.strip() for line in combined.splitlines() if line.strip()), "")
    if first_line:
        return f"{validator_name} validation failed: {first_line}"
    return f"{validator_name} validation failed with exit code {exit_code}"
