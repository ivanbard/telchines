from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from telchines.adapters.parsing import parse_common_output
from telchines.config import ProjectConfig
from telchines.models import AgentTask, CocotbCandidate, ToolReference, ValidationAttempt, VerificationRun
from telchines.providers import CocotbGenerationProviderResult, CocotbGenerationRequest, GenerationProvider
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import copy_tree_to_temp, ensure_directory, relative_to, remove_tree, stable_id, utc_now, write_json


def execute_cocotb_generation(
    config: ProjectConfig,
    store: RunStore,
    retrieval: RetrievalService,
    provider: GenerationProvider,
    dut_path: Path,
    spec_path: Path | None = None,
    output_dir: Path | None = None,
    intent: str = "",
) -> tuple[CocotbCandidate | None, VerificationRun | None, VerificationRun | None, object]:
    dut_rel = relative_to(dut_path, config.project_root)
    spec_rel = relative_to(spec_path, config.project_root) if spec_path else None
    output_dir_rel = relative_to(output_dir, config.project_root) if output_dir else _default_cocotb_output_dir(config)
    query_terms = [dut_path.stem, "cocotb", "smoke", "testbench"]
    if spec_path:
        query_terms.append(spec_path.stem)
    if intent.strip():
        query_terms.append(intent.strip())
    focus_paths = [dut_rel]
    if spec_rel:
        focus_paths.append(spec_rel)
    context = retrieval.search(query=" ".join(query_terms), mode="generation", focus_paths=focus_paths)
    store.save_context(context)

    task = AgentTask(
        task_id=stable_id("task", config.project.project_id, "dut_to_cocotb", dut_rel, spec_rel or "", intent.strip(), utc_now()),
        project_id=config.project.project_id,
        workflow_type="dut_to_cocotb",
        input_run_id=None,
        status="running",
        created_at=utc_now(),
        metadata={"dut_path": dut_rel, "spec_path": spec_rel, "output_dir": output_dir_rel, "intent": intent},
    )
    store.save_task(task)

    request = CocotbGenerationRequest(
        task_id=task.task_id,
        project_root=config.project_root,
        dut_path=dut_rel,
        spec_path=spec_rel,
        output_dir=output_dir_rel,
        intent=intent,
        retrieval_context=context,
        conventions=config.generation,
    )
    provider_result = provider.generate_cocotb(request)
    request_artifact = store.save_task_artifact(task.task_id, "cocotb_request", provider_result.request_payload)
    response_artifact = store.save_task_artifact(task.task_id, "cocotb_response", provider_result.response_payload)
    replay_artifact = store.save_task_artifact(
        task.task_id,
        "cocotb_replay",
        {
            "task_id": task.task_id,
            "provider": provider_result.provider_name,
            "context_id": context.context_id,
            "request_artifact": str(request_artifact),
            "response_artifact": str(response_artifact),
            "dut_path": dut_rel,
            "spec_path": spec_rel,
            "output_dir": output_dir_rel,
            "intent": intent,
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
        return None, None, None, context

    candidate.replay_artifacts = {
        "request_artifact": str(request_artifact),
        "response_artifact": str(response_artifact),
        "replay_artifact": str(replay_artifact),
    }
    generated_path = config.project_root / candidate.file_path
    ensure_directory(generated_path.parent)
    generated_path.write_text(candidate.candidate_content, encoding="utf-8")

    manifest_payload = _build_manifest_payload(candidate)
    manifest_path = config.project_root / candidate.manifest_path
    write_json(manifest_path, manifest_payload)

    validation_run = validate_cocotb_candidate(config, store, candidate)
    candidate.status = "validated" if validation_run.status == "passed" else "rejected"
    candidate.validation_attempts.append(
        ValidationAttempt(attempt=1, result=validation_run.status, run_id=validation_run.run_id, notes=validation_run.summary)
    )
    store.save_cocotb_candidate(candidate)

    run = VerificationRun(
        run_id=stable_id("run", candidate.candidate_id, "generation", utc_now()),
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="dut_to_cocotb",
        tool=ToolReference(kind="generator", name=provider_result.provider_name, version="configured"),
        inputs={
            "dut_path": candidate.dut_path,
            "spec_path": candidate.spec_path,
            "intent": candidate.intent,
            "output_dir": output_dir_rel,
            "provider": provider_result.provider_name,
        },
        status=candidate.status,
        started_at=utc_now(),
        finished_at=utc_now(),
        exit_code=0 if validation_run.status == "passed" else 1,
        artifacts={
            "generated_file": candidate.file_path,
            "manifest_path": candidate.manifest_path,
            "request_artifact": str(request_artifact),
            "response_artifact": str(response_artifact),
        },
        tool_result={
            "status": candidate.status,
            "top_module": candidate.top_module,
            "assumptions": candidate.assumptions,
            "port_count": len(candidate.ports),
            "validation_run_id": validation_run.run_id,
            "validation_status": validation_run.status,
        },
        summary=_generation_summary(candidate, validation_run, provider_result),
    )
    store.save_run(run)
    task.metadata["candidate_id"] = candidate.candidate_id
    task.metadata["generation_run_id"] = run.run_id
    task.metadata["validation_run_id"] = validation_run.run_id
    task.status = candidate.status
    store.save_task(task)
    return candidate, run, validation_run, context


def validate_cocotb_candidate(config: ProjectConfig, store: RunStore, candidate: CocotbCandidate) -> VerificationRun:
    run_id = stable_id("run", candidate.candidate_id, "validation", utc_now())
    temp_root = copy_tree_to_temp(config.project_root)
    try:
        target = temp_root / candidate.file_path
        ensure_directory(target.parent)
        target.write_text(candidate.candidate_content, encoding="utf-8")

        command = [sys.executable, "-m", "py_compile", str(target)]
        process = subprocess.run(command, cwd=temp_root, capture_output=True, text=True, check=False)
        structural_errors = _cocotb_structural_errors(candidate.candidate_content)
        returncode = process.returncode if process.returncode != 0 else (1 if structural_errors else 0)
        combined = process.stdout + process.stderr
        if structural_errors:
            combined = (combined + "\n" if combined else "") + "\n".join(structural_errors)

        artifacts_dir = config.project_root / config.artifacts_dir
        ensure_directory(artifacts_dir)
        log_path = artifacts_dir / f"{run_id}.log"
        log_path.write_text(combined or "py_compile: validation passed\n", encoding="utf-8")
        observations = parse_common_output(run_id, combined)
        store.save_observations(observations)
        validation_run = VerificationRun(
            run_id=run_id,
            project_id=config.project.project_id,
            commit_sha="workspace",
            workflow_type="cocotb_validation",
            tool=ToolReference(kind="validator", name="py_compile", version=f"{sys.version_info.major}.{sys.version_info.minor}"),
            inputs={
                "dut_path": candidate.dut_path,
                "spec_path": candidate.spec_path,
                "generated_file": candidate.file_path,
                "manifest_path": candidate.manifest_path,
            },
            status="passed" if returncode == 0 else "failed",
            started_at=utc_now(),
            finished_at=utc_now(),
            exit_code=returncode,
            artifacts={"log_path": str(log_path), "generated_file": candidate.file_path, "manifest_path": candidate.manifest_path},
            tool_result={
                "status": "passed" if returncode == 0 else "failed",
                "validation_mode": "python_syntax_plus_structure",
                "validators": ["py_compile", "builtin_cocotb_structure"],
                "checks": {
                    "python_syntax": "passed" if process.returncode == 0 else "failed",
                    "cocotb_import": "passed" if "import cocotb" in candidate.candidate_content else "failed",
                    "cocotb_test": "passed" if "@cocotb.test" in candidate.candidate_content else "failed",
                },
                "limitations": [
                    "built-in validation does not run a simulator",
                    "executable cocotb smoke requires optional cocotb and simulator tooling",
                ],
            },
            observation_ids=[observation.observation_id for observation in observations],
            summary=_validation_summary(returncode, combined),
            replay_command=command,
        )
        store.save_run(validation_run)
        return validation_run
    finally:
        remove_tree(temp_root)


def _build_manifest_payload(candidate: CocotbCandidate) -> dict[str, object]:
    return {
        "workflow": "dut_to_cocotb",
        "candidate_id": candidate.candidate_id,
        "dut_path": candidate.dut_path,
        "spec_path": candidate.spec_path,
        "top_module": candidate.top_module,
        "generated_file": candidate.file_path,
        "manifest_path": candidate.manifest_path,
        "intent": candidate.intent,
        "provider": candidate.provider,
        "assumptions": candidate.assumptions,
        "ports": [
            {"name": port.name, "direction": port.direction, "width": port.width, "role": port.role}
            for port in candidate.ports
        ],
        "todos": [
            "Add environment-specific monitors and scoreboard checks.",
            "Extend stimulus coverage beyond the smoke path.",
            "Connect simulator and cocotb runner configuration for executable validation.",
        ],
        "validation": {
            "mode": "python_syntax_plus_structure",
            "limitations": [
                "py_compile confirms Python syntax only.",
                "Built-in cocotb structure checks confirm import and test-decorator shape.",
                "Simulator execution requires optional cocotb and EDA tooling.",
            ],
        },
        "evidence_paths": candidate.evidence_paths,
    }


def _generation_summary(candidate: CocotbCandidate, validation_run: VerificationRun, provider_result: CocotbGenerationProviderResult) -> str:
    if validation_run.status == "passed":
        return f"{provider_result.provider_name} generated cocotb scaffold for {candidate.top_module}; python syntax validation passed"
    return f"{provider_result.provider_name} generated cocotb scaffold for {candidate.top_module}; python syntax validation failed"


def _validation_summary(exit_code: int, combined: str) -> str:
    if exit_code == 0:
        return "py_compile validation passed"
    first_line = next((line.strip() for line in combined.splitlines() if line.strip()), "")
    if first_line:
        return f"py_compile validation failed: {first_line}"
    return f"py_compile validation failed with exit code {exit_code}"


def _default_cocotb_output_dir(config: ProjectConfig) -> str:
    section = config.generation.get("cocotb", {}) if isinstance(config.generation, dict) else {}
    return str(section.get("output_dir", Path(config.artifacts_dir) / "generated" / "cocotb"))


def _cocotb_structural_errors(content: str) -> list[str]:
    errors: list[str] = []
    if "import cocotb" not in content:
        errors.append("ERROR: expected generated scaffold to import cocotb")
    if "@cocotb.test" not in content:
        errors.append("ERROR: expected at least one @cocotb.test decorator")
    return errors
