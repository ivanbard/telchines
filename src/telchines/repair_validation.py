from __future__ import annotations

import subprocess

from telchines.adapters.parsing import parse_common_output
from telchines.adapters.registry import AdapterRegistry
from telchines.config import ProjectConfig
from telchines.models import PatchProposal, VerificationRun
from telchines.run_store import RunStore
from telchines.utils import copy_tree_to_temp, ensure_directory, remove_tree, stable_id, utc_now


def validate_patch(config: ProjectConfig, store: RunStore, base_run: VerificationRun, proposal: PatchProposal, apply_patch: bool = False) -> VerificationRun:
    temp_root = copy_tree_to_temp(config.project_root)
    try:
        target = temp_root / proposal.file_path
        ensure_directory(target.parent)
        target.write_text(proposal.candidate_content, encoding="utf-8")
        run_id = stable_id("run", base_run.run_id, "validation", proposal.patch_id, utc_now())
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
        return {
            "exit_code": execution.exit_code,
            "artifacts": execution.artifacts,
            "tool_result": execution.result,
            "observations": execution.observations,
        }

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
