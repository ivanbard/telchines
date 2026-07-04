from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

from telchines.adapters.base import AdapterRunSpec
from telchines.adapters.open_tools import IcarusAdapter, VerilatorAdapter
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
    run_spec: AdapterRunSpec | None = None,
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

    max_attempts = _generation_max_attempts(config, "cocotb")
    feedback: list[dict[str, object]] = []
    attempts: list[dict[str, object]] = []
    rejected_candidate_ids: list[str] = []
    candidate: CocotbCandidate | None = None
    validation_run: VerificationRun | None = None
    provider_name = getattr(provider, "name", "")
    provider_summary = ""
    request_artifact: Path | None = None
    response_artifact: Path | None = None
    replay_artifact: Path | None = None

    for attempt in range(1, max_attempts + 1):
        request = CocotbGenerationRequest(
            task_id=task.task_id,
            project_root=config.project_root,
            dut_path=dut_rel,
            spec_path=spec_rel,
            output_dir=output_dir_rel,
            intent=intent,
            retrieval_context=context,
            conventions=config.generation,
            feedback=list(feedback),
        )
        provider_result = provider.generate_cocotb(request)
        provider_name = provider_result.provider_name
        provider_summary = provider_result.summary
        suffix = "" if attempt == 1 else f"_attempt_{attempt}"
        request_artifact = store.save_task_artifact(task.task_id, f"cocotb_request{suffix}", provider_result.request_payload)
        response_artifact = store.save_task_artifact(task.task_id, f"cocotb_response{suffix}", provider_result.response_payload)
        replay_artifact = store.save_task_artifact(
            task.task_id,
            f"cocotb_replay{suffix}",
            {
                "task_id": task.task_id,
                "provider": provider_result.provider_name,
                "attempt": attempt,
                "context_id": context.context_id,
                "request_artifact": str(request_artifact),
                "response_artifact": str(response_artifact),
                "dut_path": dut_rel,
                "spec_path": spec_rel,
                "output_dir": output_dir_rel,
                "intent": intent,
                "previous_attempts": list(feedback),
            },
        )
        candidate = provider_result.candidate
        if candidate is None:
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "no_generation",
                    "provider": provider_result.provider_name,
                    "summary": provider_result.summary,
                    "request_artifact": str(request_artifact),
                    "response_artifact": str(response_artifact),
                    "replay_artifact": str(replay_artifact),
                }
            )
            break

        candidate.candidate_id = stable_id("cocotb", task.task_id, candidate.file_path, str(attempt))
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

        validation_run = validate_cocotb_candidate(config, store, candidate, run_spec=run_spec)
        candidate.status = "validated" if validation_run.status == "passed" else "rejected"
        candidate.validation_attempts.append(
            ValidationAttempt(attempt=attempt, result=validation_run.status, run_id=validation_run.run_id, notes=validation_run.summary)
        )
        attempt_record = _attempt_record(
            attempt,
            candidate_id=candidate.candidate_id,
            provider=provider_result.provider_name,
            status=candidate.status,
            validation_run=validation_run,
            artifact_path=candidate.file_path,
            request_artifact=str(request_artifact),
            response_artifact=str(response_artifact),
            replay_artifact=str(replay_artifact),
        )
        attempts.append(attempt_record)
        candidate.attempts = list(attempts)
        candidate.rejected_candidate_ids = list(rejected_candidate_ids)
        store.save_cocotb_candidate(candidate)
        if validation_run.status == "passed":
            break
        rejected_candidate_ids.append(candidate.candidate_id)
        candidate.rejected_candidate_ids = list(rejected_candidate_ids)
        candidate.attempts = list(attempts)
        store.save_cocotb_candidate(candidate)
        feedback.append(_validation_feedback(attempt, validation_run, candidate.candidate_id, candidate.file_path))

    if candidate is None:
        task.status = "no_generation"
        task.metadata.update(
            {
                "provider": provider_name,
                "context_id": context.context_id,
                "provider_summary": provider_summary,
                "attempts": attempts,
            }
        )
        store.save_task(task)
        return None, None, None, context

    candidate.attempts = list(attempts)
    candidate.rejected_candidate_ids = list(rejected_candidate_ids)
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
            "provider": provider_name,
        },
        status=candidate.status,
        started_at=utc_now(),
        finished_at=utc_now(),
        exit_code=0 if validation_run.status == "passed" else 1,
        artifacts={
            "generated_file": candidate.file_path,
            "manifest_path": candidate.manifest_path,
            "request_artifact": str(request_artifact) if request_artifact else "",
            "response_artifact": str(response_artifact) if response_artifact else "",
        },
        tool_result={
            "status": candidate.status,
            "top_module": candidate.top_module,
            "assumptions": candidate.assumptions,
            "port_count": len(candidate.ports),
            "validation_run_id": validation_run.run_id,
            "validation_status": validation_run.status,
            "attempts": attempts,
            "rejected_candidate_ids": rejected_candidate_ids,
        },
        summary=_generation_summary(candidate, validation_run, provider_name),
    )
    store.save_run(run)
    task.metadata["candidate_id"] = candidate.candidate_id
    task.metadata["generation_run_id"] = run.run_id
    task.metadata["validation_run_id"] = validation_run.run_id
    task.metadata["attempt_count"] = len(attempts)
    task.metadata["rejected_candidate_ids"] = rejected_candidate_ids
    task.metadata["request_artifact"] = str(request_artifact) if request_artifact else None
    task.metadata["response_artifact"] = str(response_artifact) if response_artifact else None
    task.metadata["replay_artifact"] = str(replay_artifact) if replay_artifact else None
    task.metadata["provider_summary"] = provider_summary
    task.status = candidate.status
    store.save_task(task)
    return candidate, run, validation_run, context


def validate_cocotb_candidate(config: ProjectConfig, store: RunStore, candidate: CocotbCandidate, run_spec: AdapterRunSpec | None = None) -> VerificationRun:
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
        smoke = _run_executable_smoke(config, temp_root, artifacts_dir, candidate, run_spec) if returncode == 0 else _smoke_not_attempted()
        if smoke["combined"]:
            combined = (combined + "\n" if combined else "") + str(smoke["combined"])
        if smoke["exit_code"] not in (None, 0):
            returncode = int(smoke["exit_code"])
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
                "validation_mode": "compile_and_run" if smoke["executable_status"] == "passed" else "syntax_plus_structure",
                "validators": ["py_compile", "builtin_cocotb_structure"],
                "checks": {
                    "python_syntax": "passed" if process.returncode == 0 else "failed",
                    "cocotb_import": "passed" if "import cocotb" in candidate.candidate_content else "failed",
                    "cocotb_test": "passed" if "@cocotb.test" in candidate.candidate_content else "failed",
                },
                "executable_status": smoke["executable_status"],
                "simulator": smoke["simulator"],
                "command": smoke["command"],
                "command_artifacts": smoke["command_artifacts"],
                "setup_diagnostics": smoke["setup_diagnostics"],
                "limitations": [
                    *([] if smoke["executable_status"] == "passed" else ["built-in validation did not complete a simulator run"]),
                    "executable cocotb smoke requires optional cocotb, make, and simulator tooling",
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
            "mode": "syntax_plus_structure",
            "limitations": [
                "py_compile confirms Python syntax only.",
                "Built-in cocotb structure checks confirm import and test-decorator shape.",
                "Simulator execution requires optional cocotb and EDA tooling.",
            ],
        },
        "evidence_paths": candidate.evidence_paths,
    }


def _generation_summary(candidate: CocotbCandidate, validation_run: VerificationRun, provider_name: str) -> str:
    if validation_run.status == "passed":
        return f"{provider_name} generated cocotb scaffold for {candidate.top_module}; python syntax validation passed"
    return f"{provider_name} generated cocotb scaffold for {candidate.top_module}; python syntax validation failed"


def _validation_summary(exit_code: int, combined: str) -> str:
    if exit_code == 0:
        return "py_compile validation passed"
    first_line = next((line.strip() for line in combined.splitlines() if line.strip()), "")
    if first_line:
        return f"py_compile validation failed: {first_line}"
    return f"py_compile validation failed with exit code {exit_code}"


def _run_executable_smoke(
    config: ProjectConfig,
    temp_root: Path,
    artifacts_dir: Path,
    candidate: CocotbCandidate,
    run_spec: AdapterRunSpec | None,
) -> dict[str, object]:
    section = config.generation.get("cocotb", {}) if isinstance(config.generation, dict) else {}
    mode = str(section.get("executable_smoke", "auto"))
    if mode == "off":
        return _smoke_skipped(["generation.cocotb.executable_smoke is off"])
    simulator, diagnostics = _select_cocotb_simulator(str(section.get("simulator", "auto")))
    diagnostics.extend(_cocotb_common_missing())
    if diagnostics:
        status = "failed" if mode == "required" else "skipped"
        return {
            **_smoke_skipped(diagnostics),
            "executable_status": status,
            "exit_code": 1 if status == "failed" else None,
        }
    smoke_dir = ensure_directory(temp_root / ".tel" / "cocotb-smoke")
    makefile = smoke_dir / "Makefile"
    module_name = Path(candidate.file_path).stem
    spec = (run_spec or AdapterRunSpec(files=[candidate.dut_path])).expanded(temp_root)
    verilog_sources = spec.files or [candidate.dut_path]
    source_paths = [str((temp_root / path).resolve()) for path in verilog_sources]
    compile_args = [*(f"-I{path}" for path in spec.include_dirs), *(f"-D{define}" for define in spec.defines), *spec.extra_args]
    makefile.write_text(
        "\n".join(
            [
                f"SIM ?= {simulator}",
                "TOPLEVEL_LANG = verilog",
                f"VERILOG_SOURCES = {' '.join(source_paths)}",
                f"TOPLEVEL = {candidate.top_module}",
                f"MODULE = {module_name}",
                f"COMPILE_ARGS += {' '.join(compile_args)}" if compile_args else "",
                "include $(shell cocotb-config --makefiles)/Makefile.sim",
                "",
            ]
        ),
        encoding="utf-8",
    )
    command = ["make", "-f", str(makefile), f"SIM={simulator}"]
    env = os.environ.copy()
    env["PYTHONPATH"] = str((temp_root / candidate.file_path).parent.resolve()) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        result = subprocess.run(command, cwd=temp_root, env=env, capture_output=True, text=True, check=False, timeout=120)
    except subprocess.TimeoutExpired:
        result = subprocess.CompletedProcess(command, 124, "", "cocotb smoke timed out after 120 seconds\n")
    smoke_log = artifacts_dir / f"{candidate.candidate_id}_cocotb_smoke.log"
    smoke_makefile = artifacts_dir / f"{candidate.candidate_id}_cocotb_smoke.mk"
    smoke_log.write_text(result.stdout + result.stderr, encoding="utf-8")
    smoke_makefile.write_text(makefile.read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "executable_status": "passed" if result.returncode == 0 else "failed",
        "simulator": simulator,
        "exit_code": result.returncode,
        "combined": result.stdout + result.stderr,
        "command": command,
        "command_artifacts": {"cocotb_smoke_log": str(smoke_log), "cocotb_smoke_makefile": str(smoke_makefile)},
        "setup_diagnostics": [],
    }


def _select_cocotb_simulator(preferred: str) -> tuple[str | None, list[str]]:
    candidates = ["icarus", "verilator"] if preferred == "auto" else [preferred]
    for candidate in candidates:
        if candidate == "icarus" and IcarusAdapter().is_available():
            return "icarus", []
        if candidate == "verilator" and VerilatorAdapter().is_available():
            return "verilator", []
    return None, [f"no configured cocotb simulator is available: {preferred}"]


def _cocotb_common_missing() -> list[str]:
    missing: list[str] = []
    if importlib.util.find_spec("cocotb") is None:
        missing.append("python package cocotb is not installed")
    for binary in ("cocotb-config", "make"):
        if shutil.which(binary) is None:
            missing.append(f"{binary} is not available on PATH")
    return missing


def _smoke_not_attempted() -> dict[str, object]:
    return _smoke_skipped(["syntax/structure validation failed before executable smoke"])


def _smoke_skipped(diagnostics: list[str]) -> dict[str, object]:
    return {
        "executable_status": "skipped",
        "simulator": None,
        "exit_code": None,
        "combined": "",
        "command": [],
        "command_artifacts": {},
        "setup_diagnostics": diagnostics,
    }


def _default_cocotb_output_dir(config: ProjectConfig) -> str:
    section = config.generation.get("cocotb", {}) if isinstance(config.generation, dict) else {}
    return str(section.get("output_dir", Path(config.artifacts_dir) / "generated" / "cocotb"))


def _generation_max_attempts(config: ProjectConfig, section_name: str) -> int:
    section = config.generation.get(section_name, {}) if isinstance(config.generation, dict) else {}
    value = section.get("max_attempts", 1) if isinstance(section, dict) else 1
    return max(int(value), 1) if isinstance(value, int) else 1


def _attempt_record(
    attempt: int,
    *,
    candidate_id: str,
    provider: str,
    status: str,
    validation_run: VerificationRun,
    artifact_path: str,
    request_artifact: str,
    response_artifact: str,
    replay_artifact: str,
) -> dict[str, object]:
    return {
        "attempt": attempt,
        "candidate_id": candidate_id,
        "provider": provider,
        "status": status,
        "artifact_path": artifact_path,
        "validation_run_id": validation_run.run_id,
        "validation_status": validation_run.status,
        "validation_summary": validation_run.summary,
        "request_artifact": request_artifact,
        "response_artifact": response_artifact,
        "replay_artifact": replay_artifact,
    }


def _validation_feedback(attempt: int, validation_run: VerificationRun, candidate_id: str, artifact_path: str) -> dict[str, object]:
    return {
        "attempt": attempt,
        "candidate_id": candidate_id,
        "artifact_path": artifact_path,
        "validation_status": validation_run.status,
        "validation_summary": validation_run.summary,
        "observation_ids": validation_run.observation_ids,
        "tool_result": validation_run.tool_result,
    }


def _cocotb_structural_errors(content: str) -> list[str]:
    errors: list[str] = []
    if "import cocotb" not in content:
        errors.append("ERROR: expected generated scaffold to import cocotb")
    if "@cocotb.test" not in content:
        errors.append("ERROR: expected at least one @cocotb.test decorator")
    return errors
