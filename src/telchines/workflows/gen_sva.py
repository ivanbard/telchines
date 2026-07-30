from __future__ import annotations

import re
from pathlib import Path

from telchines.adapters.base import AdapterRunSpec
from telchines.adapters.registry import AdapterRegistry
from telchines.adapters.parsing import parse_common_output
from telchines.config import ProjectConfig
from telchines.errors import AdapterExecutionError
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
    run_spec: AdapterRunSpec | None = None,
) -> tuple[SvaCandidate | None, VerificationRun | None, RetrievalContext]:
    spec_rel = relative_to(spec_path, config.project_root)
    rtl_rel = relative_to(rtl_path, config.project_root)
    output_rel = relative_to(output_path, config.project_root) if output_path else _default_sva_output_path(config, rtl_path)
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

    max_attempts = _generation_max_attempts(config, "sva")
    feedback: list[dict[str, object]] = []
    attempts: list[dict[str, object]] = []
    rejected_candidate_ids: list[str] = []
    final_candidate: SvaCandidate | None = None
    final_validation: VerificationRun | None = None
    provider_name = getattr(provider, "name", "")
    provider_summary = ""
    request_artifact: Path | None = None
    response_artifact: Path | None = None
    replay_artifact: Path | None = None

    for attempt in range(1, max_attempts + 1):
        request = GenerationRequest(
            task_id=task.task_id,
            project_root=config.project_root,
            spec_path=spec_rel,
            rtl_path=rtl_rel,
            output_file=output_rel,
            retrieval_context=context,
            conventions=config.generation,
            feedback=list(feedback),
        )
        provider_result = provider.generate_sva(request)
        provider_name = provider_result.provider_name
        provider_summary = provider_result.summary
        suffix = "" if attempt == 1 else f"_attempt_{attempt}"
        request_artifact = store.save_task_artifact(task.task_id, f"sva_request{suffix}", provider_result.request_payload)
        response_artifact = store.save_task_artifact(task.task_id, f"sva_response{suffix}", provider_result.response_payload)
        replay_artifact = store.save_task_artifact(
            task.task_id,
            f"sva_replay{suffix}",
            {
                "task_id": task.task_id,
                "provider": provider_result.provider_name,
                "attempt": attempt,
                "context_id": context.context_id,
                "request_artifact": str(request_artifact),
                "response_artifact": str(response_artifact),
                "spec_path": spec_rel,
                "rtl_path": rtl_rel,
                "output_file": output_rel,
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

        candidate.candidate_id = stable_id("sva", task.task_id, candidate.file_path, str(attempt))
        candidate.replay_artifacts = {
            "request_artifact": str(request_artifact),
            "response_artifact": str(response_artifact),
            "replay_artifact": str(replay_artifact),
        }
        generated_path = config.project_root / candidate.file_path
        ensure_directory(generated_path.parent)
        generated_path.write_text(candidate.candidate_content, encoding="utf-8")

        validation_run = validate_sva_candidate(config, store, candidate, run_spec=run_spec)
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
        store.save_sva_candidate(candidate)
        final_candidate = candidate
        final_validation = validation_run
        if validation_run.status == "passed":
            break
        rejected_candidate_ids.append(candidate.candidate_id)
        candidate.rejected_candidate_ids = list(rejected_candidate_ids)
        candidate.attempts = list(attempts)
        store.save_sva_candidate(candidate)
        feedback.append(_validation_feedback(attempt, validation_run, candidate.candidate_id, candidate.file_path))

    if final_candidate is None:
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
        return None, None, context

    final_candidate.attempts = list(attempts)
    final_candidate.rejected_candidate_ids = list(rejected_candidate_ids)
    task.metadata.update(
        {
            "provider": provider_name,
            "context_id": context.context_id,
            "request_artifact": str(request_artifact) if request_artifact else None,
            "response_artifact": str(response_artifact) if response_artifact else None,
            "replay_artifact": str(replay_artifact) if replay_artifact else None,
            "provider_summary": provider_summary,
            "attempt_count": len(attempts),
            "rejected_candidate_ids": rejected_candidate_ids,
            "candidate_id": final_candidate.candidate_id,
            "validation_run_id": final_validation.run_id if final_validation else None,
        }
    )
    task.status = final_candidate.status
    store.save_sva_candidate(final_candidate)
    store.save_task(task)
    return final_candidate, final_validation, context


def validate_sva_candidate(config: ProjectConfig, store: RunStore, candidate: SvaCandidate, run_spec: AdapterRunSpec | None = None) -> VerificationRun:
    run_id = stable_id("run", candidate.candidate_id, "validation", utc_now())
    temp_root = copy_tree_to_temp(config.project_root)
    try:
        target = temp_root / candidate.file_path
        ensure_directory(target.parent)
        target.write_text(candidate.candidate_content, encoding="utf-8")

        validator_name, command, returncode, combined, tool_result = _run_validation(config, temp_root, candidate, run_spec=run_spec)
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
            tool_result=tool_result,
            observation_ids=[observation.observation_id for observation in observations],
            summary=_validation_summary(returncode, combined, validator_name, tool_result),
            replay_command=command,
        )
        store.save_run(validation_run)
        return validation_run
    finally:
        remove_tree(temp_root)


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
        "structural_status": validation_run.tool_result.get("structural_status"),
        "syntax_status": validation_run.tool_result.get("syntax_status"),
        "adapter_status": validation_run.tool_result.get("adapter_status"),
        "formal_status": validation_run.tool_result.get("formal_status"),
        "proof_status": validation_run.tool_result.get("proof_status"),
        "overall_status": validation_run.tool_result.get("overall_status"),
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


def _run_validation(
    config: ProjectConfig,
    project_root: Path,
    candidate: SvaCandidate,
    run_spec: AdapterRunSpec | None = None,
) -> tuple[str, list[str], int, str, dict[str, object]]:
    errors, checks, limitations = _builtin_sva_errors(project_root, candidate)
    command = ["builtin_sva_syntax", candidate.file_path]
    tool_result = {
        "status": "failed" if errors else "passed",
        "validation_mode": "structure_only",
        "validator": "builtin_sva_syntax",
        "checks": checks,
        "limitations": limitations,
        "structural_status": "failed" if errors else "passed",
        "syntax_status": "not_run",
        "adapter_status": "not_run",
        "formal_status": "not_run",
        "proof_status": "not_attempted",
        "overall_status": "failed" if errors else "passed",
        "formal_adapter": None,
        "command_artifacts": {},
        "setup_diagnostics": [],
    }
    if errors:
        return "builtin_sva_syntax", command, 1, "\n".join(errors), tool_result
    adapter_result = _run_adapter_validation(config, project_root, candidate, checks, run_spec=run_spec)
    base_result = adapter_result or (
        "builtin_sva_syntax",
        command,
        0,
        "builtin_sva_syntax: validation passed\n" + "\n".join(f"NOTE: {item}" for item in limitations),
        tool_result,
    )
    if base_result[2] != 0:
        return base_result
    formal_result = _run_formal_validation(config, project_root, candidate, checks, run_spec=run_spec)
    if formal_result is None:
        return base_result
    return _merge_formal_result(base_result, formal_result, required=_formal_mode(config) == "required")


def _merge_formal_result(
    base_result: tuple[str, list[str], int, str, dict[str, object]],
    formal_result: tuple[str, list[str], int, str, dict[str, object]],
    *,
    required: bool,
) -> tuple[str, list[str], int, str, dict[str, object]]:
    base_validator, base_command, base_returncode, base_output, base_tool_result = base_result
    formal_validator, formal_command, formal_returncode, formal_output, formal_tool_result = formal_result
    merged_result = dict(base_tool_result)
    merged_result.update(
        {
            "formal_status": formal_tool_result.get("formal_status", "not_run"),
            "proof_status": formal_tool_result.get("proof_status", "not_attempted"),
            "formal_adapter": formal_tool_result.get("formal_adapter"),
            "setup_diagnostics": formal_tool_result.get("setup_diagnostics", []),
            "adapter_result": formal_tool_result.get("adapter_result"),
            "command_artifacts": {
                **dict(base_tool_result.get("command_artifacts", {})),
                **dict(formal_tool_result.get("command_artifacts", {})),
            },
        }
    )
    formal_status = str(merged_result["formal_status"])
    if formal_returncode == 0:
        merged_result.update(
            {
                "status": "passed",
                "validation_mode": "formal_run",
                "validator": formal_validator,
                "overall_status": "passed",
            }
        )
        return formal_validator, formal_command, 0, formal_output or base_output, merged_result
    if required:
        merged_result.update(
            {
                "status": "failed",
                "validation_mode": "formal_run",
                "validator": formal_validator,
                "overall_status": "failed",
            }
        )
        return formal_validator, formal_command, formal_returncode, formal_output or base_output, merged_result
    if formal_status == "failed":
        merged_result.update(
            {
                "status": "passed",
                "overall_status": "passed_with_warnings",
                "limitations": [
                    *list(base_tool_result.get("limitations", [])),
                    "optional formal validation failed; required artifact validation passed, but no formal proof was established",
                ],
            }
        )
        combined = "\n".join(item for item in (base_output, formal_output) if item)
        return base_validator, base_command, base_returncode, combined, merged_result
    merged_result.update({"status": "passed", "overall_status": "passed"})
    return base_validator, base_command, base_returncode, base_output, merged_result


def _run_adapter_validation(
    config: ProjectConfig,
    project_root: Path,
    candidate: SvaCandidate,
    builtin_checks: dict[str, object],
    run_spec: AdapterRunSpec | None = None,
) -> tuple[str, list[str], int, str, dict[str, object]] | None:
    section = config.generation.get("sva", {}) if isinstance(config.generation, dict) else {}
    adapter_names = section.get("validation_adapters", ["slang", "verilator"])
    if not isinstance(adapter_names, list):
        adapter_names = []
    registry = AdapterRegistry()
    fallback_reasons: list[str] = []
    for adapter_name in [str(item) for item in adapter_names if str(item).strip()]:
        if adapter_name not in config.adapters:
            fallback_reasons.append(f"{adapter_name} is not enabled in project adapters")
            continue
        try:
            adapter = registry.get(adapter_name)
        except KeyError:
            fallback_reasons.append(f"{adapter_name} is not a registered adapter")
            continue
        if "generation_validation" not in adapter.supported_workflows:
            fallback_reasons.append(f"{adapter_name} does not support generation_validation")
            continue
        if not adapter.is_available():
            fallback_reasons.append(adapter.unavailable_message())
            continue
        try:
            adapter_spec = (run_spec or AdapterRunSpec(files=[candidate.rtl_path])).expanded(project_root)
            adapter_spec.files = [*adapter_spec.files, candidate.file_path]
            adapter_run_id = stable_id("run", candidate.candidate_id, adapter_name, "sva_adapter_validation")
            try:
                execution = adapter.run(
                    adapter_run_id,
                    project_root,
                    adapter_spec.files,
                    project_root / ".tel" / "adapter-artifacts",
                    spec=adapter_spec,
                )
            except TypeError:
                execution = adapter.run(
                    adapter_run_id,
                    project_root,
                    adapter_spec.files,
                    project_root / ".tel" / "adapter-artifacts",
                )
        except AdapterExecutionError as exc:
            fallback_reasons.append(f"{adapter_name} failed to execute: {exc}")
            continue
        combined = execution.stdout + execution.stderr
        if not combined.strip() and Path(execution.log_path).exists():
            combined = Path(execution.log_path).read_text(encoding="utf-8")
        if not combined.strip():
            combined = f"{adapter_name}: validation passed\n"
        tool_result = {
            "status": "passed" if execution.exit_code == 0 else "failed",
            "validation_mode": "adapter_backed",
            "validator": adapter_name,
            "adapter_validation_mode": adapter.validation_mode,
            "checks": {**builtin_checks, "adapter": adapter_name},
            "structural_status": "passed",
            "syntax_status": "passed" if execution.exit_code == 0 else "failed",
            "adapter_status": "passed" if execution.exit_code == 0 else "failed",
            "limitations": [
                "adapter-backed validation checks parser/lint acceptance, not assertion semantic correctness",
                "formal/simulation proof is still required for protocol confidence",
            ],
            "adapter_result": execution.result,
            "formal_status": "not_run",
            "proof_status": "not_attempted",
            "overall_status": "passed" if execution.exit_code == 0 else "failed",
            "formal_adapter": None,
            "command_artifacts": dict(execution.artifacts),
            "setup_diagnostics": [],
        }
        return adapter_name, execution.command, execution.exit_code, combined, tool_result
    if fallback_reasons:
        builtin_checks["adapter_fallback_reasons"] = fallback_reasons
    return None


def _run_formal_validation(
    config: ProjectConfig,
    project_root: Path,
    candidate: SvaCandidate,
    builtin_checks: dict[str, object],
    run_spec: AdapterRunSpec | None = None,
) -> tuple[str, list[str], int, str, dict[str, object]] | None:
    section = config.generation.get("sva", {}) if isinstance(config.generation, dict) else {}
    formal = section.get("formal", {}) if isinstance(section.get("formal", {}), dict) else {}
    mode = _formal_mode(config)
    if mode == "off":
        return None
    adapter_name = str(formal.get("adapter", "symbiyosys"))
    registry = AdapterRegistry()
    diagnostics: list[str] = []
    try:
        adapter = registry.get(adapter_name)
    except Exception:
        diagnostics.append(f"{adapter_name} is not a registered adapter")
        return _formal_setup_result(adapter_name, diagnostics, required=mode == "required")
    if adapter.name not in config.adapters:
        diagnostics.append(f"{adapter.name} is not enabled in project adapters")
    if "formal_validation" not in adapter.supported_workflows:
        diagnostics.append(f"{adapter.name} does not support formal_validation")
    if not adapter.is_available():
        diagnostics.append(f"{adapter.name} is not available on PATH")
        diagnostics.extend(adapter.setup_diagnostics())
    if diagnostics:
        return _formal_setup_result(adapter.name, diagnostics, required=mode == "required")

    formal_dir = ensure_directory(project_root / ".tel" / "formal")
    sby_path = formal_dir / f"{candidate.candidate_id}.sby"
    spec = (run_spec or AdapterRunSpec(files=[candidate.rtl_path])).expanded(project_root)
    files = [*spec.files, candidate.file_path]
    top = spec.top_module or str(builtin_checks.get("dut_module") or _extract_module_name((project_root / candidate.rtl_path).read_text(encoding="utf-8")) or Path(candidate.rtl_path).stem)
    read_lines = _sby_read_formal_lines(files, spec)
    sby_path.write_text(
        "\n".join(
            [
                "[options]",
                "mode bmc",
                "depth 4",
                "",
                "[engines]",
                "smtbmc z3",
                "",
                "[script]",
                *read_lines,
                f"prep -top {top}",
                "",
                "[files]",
                *files,
                "",
            ]
        ),
        encoding="utf-8",
    )
    formal_spec = AdapterRunSpec(
        files=[str(sby_path.relative_to(project_root)).replace("\\", "/")],
        work_library=spec.work_library,
        extra_args=spec.extra_args,
        timeout_seconds=spec.timeout_seconds,
        env=dict(spec.env),
    )
    try:
        execution = adapter.run(
            stable_id("run", candidate.candidate_id, adapter.name, "formal_validation"),
            project_root,
            formal_spec.files,
            project_root / ".tel" / "adapter-artifacts",
            spec=formal_spec,
        )
    except AdapterExecutionError as exc:
        return _formal_setup_result(adapter.name, [f"{adapter.name} failed to execute: {exc}"], required=mode == "required")
    combined = execution.stdout + execution.stderr
    if not combined.strip() and Path(execution.log_path).exists():
        combined = Path(execution.log_path).read_text(encoding="utf-8")
    tool_result = {
        "status": "passed" if execution.exit_code == 0 else "failed",
        "validation_mode": "formal_run",
        "validator": adapter.name,
        "formal_status": "passed" if execution.exit_code == 0 else "failed",
        "proof_status": "not_proved",
        "formal_adapter": adapter.name,
        "checks": {**builtin_checks, "formal_adapter": adapter.name},
        "limitations": ["bounded formal smoke depth is not a complete protocol proof"],
        "structural_status": "passed",
        "syntax_status": "not_run",
        "adapter_status": "not_run",
        "overall_status": "passed" if execution.exit_code == 0 else "failed",
        "adapter_result": execution.result,
        "command_artifacts": {"sby_file": str(sby_path), **execution.artifacts},
        "setup_diagnostics": [],
    }
    return adapter.name, execution.command, execution.exit_code, combined or f"{adapter.name}: formal validation passed\n", tool_result


def _sby_read_formal_lines(files: list[str], spec: AdapterRunSpec) -> list[str]:
    options = ["-formal", "-sv"]
    options.extend(f"-I{path}" for path in spec.include_dirs)
    options.extend(f"-D{define}" for define in spec.defines)
    option_text = " ".join(options)
    return [f"read {option_text} {Path(path).name}" for path in files]


def _formal_mode(config: ProjectConfig) -> str:
    section = config.generation.get("sva", {}) if isinstance(config.generation, dict) else {}
    formal = section.get("formal", {}) if isinstance(section.get("formal", {}), dict) else {}
    return str(formal.get("mode", "auto"))


def _formal_setup_result(adapter_name: str, diagnostics: list[str], *, required: bool) -> tuple[str, list[str], int, str, dict[str, object]] | None:
    if not required:
        return (
            adapter_name,
            [],
            2,
            "",
            {
                "status": "skipped",
                "validation_mode": "formal_run",
                "formal_status": "skipped",
                "proof_status": "not_attempted",
                "formal_adapter": adapter_name,
                "checks": {},
                "limitations": ["formal execution skipped because required tooling is unavailable"],
                "command_artifacts": {},
                "setup_diagnostics": diagnostics,
            },
        )
    combined = "\n".join(f"ERROR: {item}" for item in diagnostics)
    return (
        adapter_name,
        [],
        1,
        combined,
        {
            "status": "failed",
                "validation_mode": "formal_run",
                "formal_status": "failed",
                "proof_status": "not_proved",
            "formal_adapter": adapter_name,
            "checks": {},
            "limitations": [],
            "command_artifacts": {},
            "setup_diagnostics": diagnostics,
        },
    )


def _builtin_sva_errors(project_root: Path, candidate: SvaCandidate) -> tuple[list[str], dict[str, object], list[str]]:
    content = candidate.candidate_content
    errors: list[str] = []
    checks: dict[str, object] = {}
    limitations = [
        "built-in checks validate structure and obvious DUT references only",
        "assertion semantics require adapter-backed parser/formal/simulation validation",
    ]
    module_count = len(re.findall(r"(?m)^\s*module\b", content))
    endmodule_count = len(re.findall(r"(?m)^\s*endmodule\b", content))
    property_count = len(re.findall(r"(?m)^\s*property\b", content))
    endproperty_count = len(re.findall(r"(?m)^\s*endproperty\b", content))
    checks["module_count"] = module_count
    checks["property_count"] = property_count
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
    bind_errors, bind_checks, bind_limitations = _builtin_bind_errors(project_root, candidate)
    errors.extend(bind_errors)
    checks.update(bind_checks)
    limitations.extend(bind_limitations)
    return errors, checks, limitations


def _builtin_bind_errors(project_root: Path, candidate: SvaCandidate) -> tuple[list[str], dict[str, object], list[str]]:
    rtl_path = project_root / candidate.rtl_path
    if not rtl_path.exists():
        return [f"ERROR: RTL file not found for bind validation: {candidate.rtl_path}"], {"bind_validation": "failed"}, []
    rtl_content = rtl_path.read_text(encoding="utf-8")
    dut_module = _extract_module_name(rtl_content) or Path(candidate.rtl_path).stem
    dut_identifiers = _extract_sv_identifiers(rtl_content)
    generated_modules = set(_extract_module_names(candidate.candidate_content))
    binds = _extract_bind_statements(candidate.candidate_content)
    checks: dict[str, object] = {
        "dut_module": dut_module,
        "dut_identifier_count": len(dut_identifiers),
        "bind_count": len(binds),
    }
    if not binds:
        return [], checks, ["no bind statement found; DUT attachment could not be validated"]
    errors: list[str] = []
    for bind in binds:
        if bind["target"] != dut_module:
            errors.append(f"ERROR: bind target `{bind['target']}` does not match DUT module `{dut_module}`")
        if bind["checker"] not in generated_modules:
            errors.append(f"ERROR: bind checker module `{bind['checker']}` is not defined in generated SVA artifact")
        for actual in bind["actuals"]:
            for token in _sv_identifier_tokens(actual):
                if token not in dut_identifiers:
                    errors.append(f"ERROR: bind references unknown DUT signal `{token}`")
    return errors, checks, []


def _default_sva_output_path(config: ProjectConfig, rtl_path: Path) -> str:
    section = config.generation.get("sva", {}) if isinstance(config.generation, dict) else {}
    output_dir = str(section.get("output_dir", Path(config.artifacts_dir) / "generated"))
    template = str(section.get("filename_template", "{module}_assertions.sv"))
    rtl_content = rtl_path.read_text(encoding="utf-8") if rtl_path.exists() else ""
    module_name = _extract_module_name(rtl_content) or rtl_path.stem
    filename = template.format(module=module_name, rtl_stem=rtl_path.stem, dut_stem=rtl_path.stem)
    return (Path(output_dir) / filename).as_posix()


def _extract_module_name(content: str) -> str | None:
    match = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", content)
    return match.group(1) if match else None


def _extract_module_names(content: str) -> list[str]:
    return re.findall(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:#\s*\(|\()", content)


def _extract_bind_statements(content: str) -> list[dict[str, object]]:
    binds: list[dict[str, object]] = []
    pattern = re.compile(
        r"\bbind\s+(?P<target>[A-Za-z_][A-Za-z0-9_]*)\s+"
        r"(?P<checker>[A-Za-z_][A-Za-z0-9_]*)\s+"
        r"(?P<instance>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<connections>.*?)\)\s*;",
        re.DOTALL,
    )
    for match in pattern.finditer(content):
        actuals = [item.group("actual").strip() for item in re.finditer(r"\.[A-Za-z_][A-Za-z0-9_]*\s*\(\s*(?P<actual>[^)]+)\)", match.group("connections"))]
        binds.append(
            {
                "target": match.group("target"),
                "checker": match.group("checker"),
                "instance": match.group("instance"),
                "actuals": actuals,
            }
        )
    return binds


def _extract_sv_identifiers(content: str) -> set[str]:
    identifiers: set[str] = set()
    for match in re.finditer(r"\bmodule\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<header>.*?)\)\s*;", content, re.DOTALL):
        identifiers.add(match.group("name"))
        for port_match in re.finditer(r"\b(?:input|output|inout)\b(?P<body>[^,\n;]*(?:,[^,\n;]*)*)", match.group("header")):
            identifiers.update(_names_from_sv_decl_body(port_match.group("body")))
    for decl_match in re.finditer(r"\b(?:logic|wire|reg|bit)\b(?P<body>[^;]+);", content):
        identifiers.update(_names_from_sv_decl_body(decl_match.group("body")))
    return identifiers


def _names_from_sv_decl_body(body: str) -> set[str]:
    cleaned = re.sub(r"\[[^\]]+\]", " ", body)
    cleaned = re.sub(r"\b(?:logic|wire|reg|bit|signed|unsigned|input|output|inout)\b", " ", cleaned)
    names: set[str] = set()
    for part in cleaned.split(","):
        name = part.strip().split("=")[0].strip()
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", name)
        if match:
            names.add(match.group(1))
    return names


def _sv_identifier_tokens(value: str) -> list[str]:
    keywords = {"and", "or", "not", "posedge", "negedge", "if", "else", "begin", "end", "true", "false"}
    return [token for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", value) if token.lower() not in keywords]


def _validation_summary(exit_code: int, combined: str, validator_name: str, tool_result: dict[str, object]) -> str:
    if exit_code == 0:
        if tool_result.get("overall_status") == "passed_with_warnings":
            return f"{validator_name} required validation passed with formal-validation warnings"
        return f"{validator_name} validation passed"
    first_line = next((line.strip() for line in combined.splitlines() if line.strip()), "")
    if first_line:
        return f"{validator_name} validation failed: {first_line}"
    return f"{validator_name} validation failed with exit code {exit_code}"
