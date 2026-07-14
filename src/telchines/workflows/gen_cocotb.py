from __future__ import annotations

import importlib.util
import os
import signal
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from telchines.adapters.base import AdapterRunSpec
from telchines.adapters.open_tools import IcarusAdapter, VerilatorAdapter
from telchines.adapters.parsing import parse_common_output
from telchines.config import ProjectConfig
from telchines.models import AgentTask, CocotbCandidate, ToolReference, ValidationAttempt, VerificationRun
from telchines.providers import CocotbGenerationProviderResult, CocotbGenerationRequest, GenerationProvider
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import SECRET_KEY_RE, copy_tree_to_temp, ensure_directory, relative_to, remove_tree, stable_id, utc_now, write_json


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
        write_json(manifest_path, _build_manifest_payload(candidate, validation_run=validation_run))
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
        try:
            process = subprocess.run(command, cwd=temp_root, capture_output=True, text=True, check=False, timeout=30)
        except subprocess.TimeoutExpired:
            process = subprocess.CompletedProcess(command, 124, "", "py_compile validation timed out after 30 seconds\n")
        structural_errors = _cocotb_structural_errors(candidate.candidate_content)
        returncode = process.returncode if process.returncode != 0 else (1 if structural_errors else 0)
        combined = process.stdout + process.stderr
        if structural_errors:
            combined = (combined + "\n" if combined else "") + "\n".join(structural_errors)

        artifacts_dir = config.project_root / config.artifacts_dir
        ensure_directory(artifacts_dir)
        base_stages = {
            "python_syntax": _validation_stage(
                "passed" if process.returncode == 0 else "failed",
                [_first_meaningful_line(process.stderr or process.stdout)] if process.returncode != 0 else [],
                command=command,
                exit_code=process.returncode,
            ),
            "structural_validation": _validation_stage("passed" if not structural_errors else "failed", structural_errors),
        }
        smoke = _run_executable_smoke(config, temp_root, artifacts_dir, candidate, run_spec) if returncode == 0 else _smoke_not_attempted()
        stages = {**base_stages, **dict(smoke["stages"])}
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
                "executable_contract": smoke["executable_contract"],
                "stages": stages,
                "simulator": smoke["simulator"],
                "command": smoke["command"],
                "command_artifacts": smoke["command_artifacts"],
                "environment_summary": smoke["environment_summary"],
                "setup_diagnostics": smoke["setup_diagnostics"],
                "limitations": [
                    *([] if smoke["executable_status"] == "passed" else ["built-in validation did not complete a simulator run"]),
                    *(
                        []
                        if smoke["executable_status"] == "passed"
                        else ["executable cocotb smoke requires optional cocotb, make, and simulator tooling"]
                    ),
                ],
            },
            observation_ids=[observation.observation_id for observation in observations],
            summary=_validation_summary(returncode, combined, stages=stages, executable_status=str(smoke["executable_status"])),
            replay_command=command,
        )
        store.save_run(validation_run)
        return validation_run
    finally:
        remove_tree(temp_root)


def _build_manifest_payload(candidate: CocotbCandidate, validation_run: VerificationRun | None = None) -> dict[str, object]:
    validation: dict[str, object] = {
        "mode": "syntax_plus_structure",
        "limitations": [
            "py_compile confirms Python syntax only.",
            "Built-in cocotb structure checks confirm import and test-decorator shape.",
            "Simulator execution requires optional cocotb and EDA tooling.",
        ],
    }
    if validation_run is not None:
        validation = {
            "run_id": validation_run.run_id,
            "status": validation_run.status,
            "summary": validation_run.summary,
            "mode": validation_run.tool_result.get("validation_mode", "syntax_plus_structure"),
            "executable_status": validation_run.tool_result.get("executable_status"),
            "executable_contract": validation_run.tool_result.get("executable_contract"),
            "stages": validation_run.tool_result.get("stages", {}),
            "simulator": validation_run.tool_result.get("simulator"),
            "command_artifacts": validation_run.tool_result.get("command_artifacts", {}),
            "setup_diagnostics": validation_run.tool_result.get("setup_diagnostics", []),
            "limitations": validation_run.tool_result.get("limitations", []),
        }
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
        "validation": validation,
        "evidence_paths": candidate.evidence_paths,
    }


def _generation_summary(candidate: CocotbCandidate, validation_run: VerificationRun, provider_name: str) -> str:
    if validation_run.status == "passed":
        return f"{provider_name} generated cocotb scaffold for {candidate.top_module}; {validation_run.summary}"
    return f"{provider_name} generated cocotb scaffold for {candidate.top_module}; validation failed: {validation_run.summary}"


def _validation_summary(
    exit_code: int,
    combined: str,
    *,
    stages: dict[str, object],
    executable_status: str = "not_attempted",
) -> str:
    stage_labels = {
        "python_syntax": "Python syntax validation",
        "structural_validation": "cocotb structural validation",
        "simulator_compile": "simulator compile",
        "simulator_launch": "simulator launch",
        "cocotb_init": "cocotb initialization",
        "test_results": "cocotb test results",
    }
    for stage_name, label in stage_labels.items():
        stage = stages.get(stage_name, {})
        if isinstance(stage, dict) and stage.get("status") == "failed":
            diagnostics = stage.get("diagnostics", [])
            detail = next((str(item) for item in diagnostics if str(item)), "")
            return f"{label} failed{': ' + detail if detail else ''}"
    if exit_code == 0:
        if executable_status == "passed":
            return "py_compile and executable cocotb smoke validation passed"
        skipped = stages.get("simulator_compile", {})
        diagnostics = skipped.get("diagnostics", []) if isinstance(skipped, dict) else []
        detail = next((str(item) for item in diagnostics if str(item)), "")
        return f"py_compile and cocotb structural validation passed; executable smoke skipped{': ' + detail if detail else ''}"
    first_line = _first_meaningful_line(combined)
    return f"cocotb validation failed{': ' + first_line if first_line else f' with exit code {exit_code}'}"


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
        return _smoke_skipped(["generation.cocotb.executable_smoke is off"], executable_contract="disabled")
    simulator, diagnostics = _select_cocotb_simulator(str(section.get("simulator", "auto")))
    diagnostics.extend(_cocotb_common_missing())
    makefiles_dir, makefiles_diagnostics = _cocotb_makefiles_dir()
    diagnostics.extend(makefiles_diagnostics)
    contract_status = "unsupported"
    if not diagnostics:
        contract_status, contract_diagnostics = _cocotb_execution_contract(simulator)
        diagnostics.extend(contract_diagnostics)
    if diagnostics:
        status = "failed" if mode == "required" else "skipped"
        return {
            **_smoke_skipped(diagnostics, executable_contract=contract_status),
            "executable_status": status,
            "exit_code": 1 if status == "failed" else None,
        }
    smoke_dir = ensure_directory(temp_root / ".tel" / "cocotb-smoke")
    makefile = smoke_dir / "Makefile"
    module_name = Path(candidate.file_path).stem
    spec = (run_spec or AdapterRunSpec(files=[candidate.dut_path])).expanded(temp_root)
    verilog_sources = spec.files or [candidate.dut_path]
    source_paths = [_makefile_path((temp_root / path).resolve()) for path in verilog_sources]
    compile_args = [*(f"-I{path}" for path in spec.include_dirs), *(f"-D{define}" for define in spec.defines), *spec.extra_args]
    toplevel = spec.top_module or candidate.top_module
    makefile_sim = _makefile_path(makefiles_dir / "Makefile.sim")
    sim_build = smoke_dir / "sim_build"
    results_file = smoke_dir / "results.xml"
    makefile.write_text(
        "\n".join(
            [
                f"SIM ?= {simulator}",
                "TOPLEVEL_LANG = verilog",
                f"VERILOG_SOURCES = {' '.join(source_paths)}",
                f"TOPLEVEL = {toplevel}",
                f"MODULE = {module_name}",
                f"SIM_BUILD = {_makefile_path(sim_build)}",
                f"COCOTB_RESULTS_FILE = {_makefile_path(results_file)}",
                f"COMPILE_ARGS += {' '.join(compile_args)}" if compile_args else "",
                f"include {makefile_sim}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    python_bin = _cocotb_python_bin_for_make()
    pygpi_python_bin = _native_cocotb_python_bin()
    make_command = [
        "make",
        "-f",
        _makefile_path(makefile),
        f"SIM={simulator}",
        f"PYTHON_BIN={python_bin}",
        f"PYGPI_PYTHON_BIN={pygpi_python_bin}",
    ]
    compile_command = [*make_command, _makefile_path(sim_build / "sim.vvp")]
    run_command = [*make_command, _makefile_path(results_file)]
    env = os.environ.copy()
    env.update(spec.env)
    _prepend_cocotb_smoke_path(env)
    env["PYGPI_PYTHON_BIN"] = pygpi_python_bin
    env["PYTHONPATH"] = str((temp_root / candidate.file_path).parent.resolve()) + os.pathsep + env.get("PYTHONPATH", "")
    environment_summary = _cocotb_environment_summary(spec.env, env["PYTHONPATH"], temp_root)
    environment_summary["COCOTB_MAKEFILES"] = _summarize_path(str(makefiles_dir), temp_root)
    environment_summary["PYTHON_BIN"] = python_bin
    environment_summary["PYGPI_PYTHON_BIN"] = pygpi_python_bin
    try:
        compile_result = _run_cocotb_smoke_command(compile_command, cwd=temp_root, env=env, timeout=120)
    except OSError as exc:
        compile_result = subprocess.CompletedProcess(compile_command, 125, "", f"simulator compile could not start: {exc}\n")
    compile_output = compile_result.stdout + compile_result.stderr
    stages = _smoke_stages_after_compile(compile_result.returncode, compile_output, compile_command)
    run_result: subprocess.CompletedProcess[str] | None = None
    if compile_result.returncode == 0:
        try:
            run_result = _run_cocotb_smoke_command(run_command, cwd=temp_root, env=env, timeout=120)
        except OSError as exc:
            run_result = subprocess.CompletedProcess(run_command, 125, "", f"cocotb smoke could not start: {exc}\n")
        run_output = run_result.stdout + run_result.stderr
        stages.update(_smoke_stages_after_run(run_result.returncode, run_output, run_command, results_file))
    else:
        run_output = ""
    combined = compile_output + (("\n" if compile_output and run_output else "") + run_output)
    smoke_log = artifacts_dir / f"{candidate.candidate_id}_cocotb_smoke.log"
    smoke_makefile = artifacts_dir / f"{candidate.candidate_id}_cocotb_smoke.mk"
    smoke_compile_log = artifacts_dir / f"{candidate.candidate_id}_cocotb_compile.log"
    smoke_run_log = artifacts_dir / f"{candidate.candidate_id}_cocotb_run.log"
    smoke_log.write_text(combined, encoding="utf-8")
    smoke_compile_log.write_text(compile_output, encoding="utf-8")
    smoke_run_log.write_text(run_output, encoding="utf-8")
    smoke_makefile.write_text(makefile.read_text(encoding="utf-8"), encoding="utf-8")
    raw_exit_code = run_result.returncode if run_result is not None else compile_result.returncode
    stage_failed = any(
        isinstance(stage, dict) and stage.get("status") == "failed"
        for stage in stages.values()
    )
    exit_code = raw_exit_code if raw_exit_code != 0 else (1 if stage_failed else 0)
    return {
        "executable_status": "passed" if exit_code == 0 else "failed",
        "executable_contract": "supported",
        "simulator": simulator,
        "exit_code": exit_code,
        "raw_exit_code": raw_exit_code,
        "combined": combined,
        "command": run_command,
        "commands": {"simulator_compile": compile_command, "simulator_launch": run_command},
        "command_artifacts": {
            "cocotb_smoke_log": str(smoke_log),
            "cocotb_smoke_makefile": str(smoke_makefile),
            "cocotb_compile_log": str(smoke_compile_log),
            "cocotb_run_log": str(smoke_run_log),
        },
        "environment_summary": environment_summary,
        "setup_diagnostics": [],
        "stages": stages,
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
        missing.append('python package cocotb is not installed; run python -m pip install -e ".[cocotb-smoke]"')
    if shutil.which("make") is None:
        missing.append("make is not available on PATH")
    return missing


def _cocotb_execution_contract(simulator: str | None) -> tuple[str, list[str]]:
    """Return whether this host can attempt a cocotb simulator execution safely."""
    if simulator is None:
        return "unsupported", ["no cocotb simulator was selected"]
    diagnostics: list[str] = []
    if simulator == "icarus" and shutil.which("vvp") is None:
        diagnostics.append("simulator launch prerequisite missing: vvp is not available on PATH")
    if simulator == "verilator" and shutil.which("verilator") is None:
        diagnostics.append("simulator compile prerequisite missing: verilator is not available on PATH")
    if _make_uses_msys() and shutil.which("cygpath") is None:
        diagnostics.append("unsupported Windows/MSYS cocotb setup: MSYS make requires cygpath for simulator source paths")
    if os.name == "nt" and _make_uses_msys() and simulator == "icarus":
        diagnostics.append(
            "unsupported Windows/MSYS/native-Icarus cocotb execution: cocotb's Makefile runner cannot reliably initialize Python in native vvp; use WSL/Linux or a separately validated non-MSYS simulator setup"
        )
    command = [sys.executable, "-m", "cocotb_tools.config", "--lib-name-path", "vpi", simulator]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        diagnostics.append(f"cocotb simulator binding probe failed for {simulator}: {exc}")
    else:
        if result.returncode != 0 or not result.stdout.strip():
            detail = _first_meaningful_line(result.stderr or result.stdout)
            suffix = f": {detail}" if detail else ""
            diagnostics.append(f"cocotb does not provide a usable VPI binding for simulator {simulator}{suffix}")
    return ("supported" if not diagnostics else "unsupported"), diagnostics


def _run_cocotb_smoke_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run one simulator phase and reap all descendants if its timeout expires."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        _terminate_cocotb_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired as cleanup_exc:
            process.kill()
            stdout = _timeout_output(cleanup_exc.stdout)
            stderr = _timeout_output(cleanup_exc.stderr)
        timed_stdout = _timeout_output(exc.stdout)
        timed_stderr = _timeout_output(exc.stderr)
        return subprocess.CompletedProcess(
            command,
            124,
            stdout or timed_stdout,
            (stderr or timed_stderr) + f"cocotb smoke phase timed out after {timeout} seconds; terminated the process tree\n",
        )


def _terminate_cocotb_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            if result.returncode != 0:
                process.kill()
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        process.kill()


def _timeout_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _cocotb_makefiles_dir() -> tuple[Path, list[str]]:
    command = ["cocotb-config", "--makefiles"] if shutil.which("cocotb-config") else [sys.executable, "-m", "cocotb_tools.config", "--makefiles"]
    if command[0] == sys.executable and importlib.util.find_spec("cocotb_tools.config") is None:
        return Path(), ["cocotb makefiles are not discoverable via cocotb-config or python -m cocotb_tools.config --makefiles"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Path(), [f"failed to query cocotb makefiles with {' '.join(command)}: {exc}"]
    makefiles = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if result.returncode != 0 or not makefiles:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        return Path(), [f"failed to query cocotb makefiles with {' '.join(command)}{suffix}"]
    makefiles_dir = Path(makefiles)
    if not (makefiles_dir / "Makefile.sim").exists():
        return Path(), [f"cocotb Makefile.sim was not found under {makefiles_dir}"]
    return makefiles_dir, []


def _prepend_cocotb_smoke_path(env: dict[str, str]) -> None:
    entries: list[str] = []
    make_path = shutil.which("make")
    if make_path:
        entries.append(str(Path(make_path).resolve().parent))
    entries.append(str(Path(sys.executable).resolve().parent))
    existing = env.get("PATH", "")
    env["PATH"] = os.pathsep.join([*entries, existing]) if existing else os.pathsep.join(entries)


def _cocotb_python_bin_for_make() -> str:
    return _makefile_path(Path(sys.executable).resolve())


def _native_cocotb_python_bin() -> str:
    return Path(sys.executable).resolve().as_posix()


def _makefile_path(path: Path) -> str:
    original = path.as_posix()
    if os.name == "nt" and original.startswith("/") and not original.startswith("//"):
        return original
    resolved = path.resolve()
    if _make_uses_msys():
        converted = _cygpath(resolved)
        if converted:
            return converted
    return resolved.as_posix()


def _make_uses_msys() -> bool:
    make_path = shutil.which("make") or ""
    normalized = make_path.replace("\\", "/").lower()
    return "msys" in normalized or "mingw" in normalized


def _cygpath(path: Path) -> str:
    cygpath = shutil.which("cygpath")
    if cygpath is None:
        return ""
    try:
        result = subprocess.run([cygpath, "-u", str(path)], capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _validation_stage(
    status: str,
    diagnostics: list[str] | None = None,
    *,
    command: list[str] | None = None,
    exit_code: int | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "diagnostics": [item for item in (diagnostics or []) if item],
        "command": command or [],
        "exit_code": exit_code,
    }


def _skipped_simulator_stages(
    diagnostics: list[str],
    stage_names: tuple[str, ...] = ("simulator_compile", "simulator_launch", "cocotb_init", "test_results"),
) -> dict[str, dict[str, object]]:
    return {
        stage: _validation_stage("skipped", diagnostics)
        for stage in stage_names
    }


def _smoke_stages_after_compile(exit_code: int, output: str, command: list[str]) -> dict[str, dict[str, object]]:
    if exit_code == 0:
        return {"simulator_compile": _validation_stage("passed", command=command, exit_code=exit_code)}
    diagnostic = _first_meaningful_line(output) or f"make exited with code {exit_code} while compiling the simulator model"
    return {
        "simulator_compile": _validation_stage("failed", [diagnostic], command=command, exit_code=exit_code),
        **_skipped_simulator_stages(
            ["simulator compile failed before simulator launch"],
            ("simulator_launch", "cocotb_init", "test_results"),
        ),
    }


def _smoke_stages_after_run(
    exit_code: int,
    output: str,
    command: list[str],
    results_file: Path,
) -> dict[str, dict[str, object]]:
    lowered = output.lower()
    launch_failed = "no rule to make target" in lowered or "make: ***" in lowered and "vvp" not in lowered
    if launch_failed:
        diagnostic = _first_meaningful_line(output) or f"make exited with code {exit_code} before simulator launch"
        return {
            "simulator_launch": _validation_stage("failed", [diagnostic], command=command, exit_code=exit_code),
            **_skipped_simulator_stages(
                ["simulator launch failed before cocotb initialization"],
                ("cocotb_init", "test_results"),
            ),
        }

    init_errors = (
        "unexpected sys.executable value",
        "runtimeerror: no simulator available",
        "failed to initialize cocotb",
        "cocotb initialization failed",
    )
    if any(marker in lowered for marker in init_errors):
        diagnostic = next((line.strip() for line in output.splitlines() if any(marker in line.lower() for marker in init_errors)), "cocotb initialization failed")
        return {
            "simulator_launch": _validation_stage("passed", command=command, exit_code=exit_code),
            "cocotb_init": _validation_stage("failed", [diagnostic], command=command, exit_code=exit_code),
            "test_results": _validation_stage("skipped", ["cocotb initialization failed before tests ran"]),
        }

    result_status, result_diagnostics = _cocotb_results_status(results_file)
    if result_status is not None:
        return {
            "simulator_launch": _validation_stage("passed", command=command, exit_code=exit_code),
            "cocotb_init": _validation_stage("passed", command=command, exit_code=exit_code),
            "test_results": _validation_stage(result_status, result_diagnostics, command=command, exit_code=exit_code),
        }

    if exit_code == 0:
        return {
            "simulator_launch": _validation_stage("passed", command=command, exit_code=exit_code),
            "cocotb_init": _validation_stage("passed", command=command, exit_code=exit_code),
            "test_results": _validation_stage("passed", command=command, exit_code=exit_code),
        }

    diagnostic = _first_meaningful_line(output) or f"make exited with code {exit_code} without producing cocotb results"
    return {
        "simulator_launch": _validation_stage("passed", command=command, exit_code=exit_code),
        "cocotb_init": _validation_stage("failed", [diagnostic], command=command, exit_code=exit_code),
        "test_results": _validation_stage("skipped", ["cocotb did not initialize far enough to report test results"]),
    }


def _cocotb_results_status(results_file: Path) -> tuple[str | None, list[str]]:
    if not results_file.exists():
        return None, []
    try:
        root = ET.parse(results_file).getroot()
    except (ET.ParseError, OSError) as exc:
        return "failed", [f"unable to parse cocotb result file: {exc}"]
    failures = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] in {"failure", "error"}]
    if failures:
        detail = next((element.get("message") or (element.text or "").strip() for element in failures if element.get("message") or (element.text or "").strip()), "cocotb reported a failing test")
        return "failed", [detail]
    return "passed", []


def _first_meaningful_line(output: str) -> str:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("rm -f "):
            return stripped
    return ""


def _smoke_not_attempted() -> dict[str, object]:
    return _smoke_skipped(["syntax/structure validation failed before executable smoke"], executable_contract="not_evaluated")


def _smoke_skipped(diagnostics: list[str], *, executable_contract: str = "unsupported") -> dict[str, object]:
    return {
        "executable_status": "skipped",
        "executable_contract": executable_contract,
        "simulator": None,
        "exit_code": None,
        "combined": "",
        "command": [],
        "command_artifacts": {},
        "environment_summary": {},
        "setup_diagnostics": diagnostics,
        "stages": _skipped_simulator_stages(diagnostics),
    }


def _cocotb_environment_summary(run_env: dict[str, str], pythonpath: str, temp_root: Path) -> dict[str, str]:
    summary = {key: ("<redacted>" if SECRET_KEY_RE.search(str(key)) else str(value)) for key, value in run_env.items()}
    entries = [entry for entry in pythonpath.split(os.pathsep) if entry]
    if entries:
        first = _summarize_path(entries[0], temp_root)
        inherited = len(entries) - 1
        summary["PYTHONPATH"] = first if inherited == 0 else f"{first}{os.pathsep}<inherited:{inherited}>"
    return summary


def _summarize_path(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return path.replace("\\", "/")


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
