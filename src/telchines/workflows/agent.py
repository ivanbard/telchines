from __future__ import annotations

from pathlib import Path
from typing import Any

from telchines.adapters.registry import AdapterRegistry
from telchines.config import ProjectConfig
from telchines.models import AgentTask, VerificationRun
from telchines.providers import build_generation_provider, build_repair_provider
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import dataclass_to_dict, relative_to, stable_id, utc_now
from telchines.workflows.coverage import execute_coverage_plan
from telchines.workflows.gen_cocotb import execute_cocotb_generation
from telchines.workflows.gen_sva import execute_generation
from telchines.workflows.repair import execute_repair
from telchines.workflows.triage import triage_logs


def execute_agent(
    config: ProjectConfig,
    store: RunStore,
    retrieval: RetrievalService,
    task: str,
    *,
    tool: str | None = None,
    files: list[str] | None = None,
    extra_args: list[str] | None = None,
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
) -> dict[str, object]:
    task_text = task.strip()
    if not task_text:
        raise ValueError("agent task must not be empty")

    files = files or []
    extra_args = extra_args or []
    logs = logs or []
    waveforms = waveforms or []
    rtl = rtl or []
    spec = spec or []

    workflow_type = _select_workflow(task_text, tool=tool, files=files, logs=logs, report=report, rtl=rtl, spec=spec, dut=dut)
    agent_task = AgentTask(
        task_id=stable_id("task", config.project.project_id, "agent", workflow_type, task_text, utc_now()),
        project_id=config.project.project_id,
        workflow_type="agent",
        input_run_id=None,
        status="running",
        created_at=utc_now(),
        metadata={
            "requested_workflow_type": workflow_type,
            "review_gated": not apply_patch,
        },
    )
    store.save_task(agent_task)

    context = retrieval.search(
        query=_agent_retrieval_query(task_text, files=files, logs=logs, report=report, rtl=rtl, spec=spec, dut=dut),
        limit=int(config.retrieval.get("max_hits", 5)),
        mode="agent",
        focus_paths=_focus_paths(config, files=files, logs=logs, report=report, rtl=rtl, spec=spec, dut=dut),
    )
    store.save_context(context)

    plan = _build_plan(workflow_type, review_required=not apply_patch)
    request_artifact = store.save_task_artifact(
        agent_task.task_id,
        "agent_request",
        {
            "task": task_text,
            "workflow_type": workflow_type,
            "options": {
                "tool": tool,
                "files": files,
                "extra_args": extra_args,
                "apply_patch": apply_patch,
                "logs": [relative_to(path, config.project_root) for path in logs],
                "waveforms": [relative_to(path, config.project_root) for path in waveforms],
                "report": relative_to(report, config.project_root) if report else None,
                "exclusions": relative_to(exclusions, config.project_root) if exclusions else None,
                "formal_run_id": formal_run_id,
                "rtl": [relative_to(path, config.project_root) for path in rtl],
                "spec": [relative_to(path, config.project_root) for path in spec],
                "dut": relative_to(dut, config.project_root) if dut else None,
                "output": relative_to(output, config.project_root) if output else None,
                "output_dir": relative_to(output_dir, config.project_root) if output_dir else None,
                "provider": provider_name,
                "intent": intent,
            },
            "context_id": context.context_id,
        },
    )
    plan_artifact = store.save_task_artifact(agent_task.task_id, "agent_plan", plan)

    steps: list[dict[str, object]] = [
        {
            "step": "retrieve_context",
            "status": "passed",
            "context_id": context.context_id,
            "hit_count": len(context.hits),
            "citations": [hit.citation for hit in context.hits],
        }
    ]
    result = _execute_selected_workflow(
        config,
        store,
        retrieval,
        workflow_type,
        tool=tool,
        files=files,
        extra_args=extra_args,
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
        intent=intent or task_text,
    )
    steps.extend(result["steps"])

    status = _agent_status(result, apply_patch=apply_patch)
    response: dict[str, object] = {
        "task_id": agent_task.task_id,
        "status": status,
        "task": task_text,
        "workflow_type": workflow_type,
        "context_id": context.context_id,
        "plan": plan,
        "steps": steps,
        "review_gate": {
            "required": not apply_patch and workflow_type in {"compile_repair", "spec_to_sva", "dut_to_cocotb"},
            "applied": apply_patch if workflow_type == "compile_repair" else False,
            "summary": _review_gate_summary(workflow_type, status, apply_patch),
        },
        "evidence": {
            "context_id": context.context_id,
            **result["evidence"],
        },
        "result": result["payload"],
    }
    response_artifact = store.save_task_artifact(agent_task.task_id, "agent_response", response)
    replay_artifact = store.save_task_artifact(
        agent_task.task_id,
        "agent_replay",
        {
            "task_id": agent_task.task_id,
            "task": task_text,
            "workflow_type": workflow_type,
            "request_artifact": str(request_artifact),
            "plan_artifact": str(plan_artifact),
            "response_artifact": str(response_artifact),
            "context_id": context.context_id,
            "lower_level_replay_artifacts": result["evidence"].get("replay_artifacts", {}),
        },
    )

    response["replay_artifacts"] = {
        "request_artifact": str(request_artifact),
        "plan_artifact": str(plan_artifact),
        "response_artifact": str(response_artifact),
        "replay_artifact": str(replay_artifact),
    }
    store.save_task_artifact(agent_task.task_id, "agent_response", response)
    agent_task.status = status
    agent_task.metadata.update(
        {
            "workflow_type": workflow_type,
            "context_id": context.context_id,
            "request_artifact": str(request_artifact),
            "plan_artifact": str(plan_artifact),
            "response_artifact": str(response_artifact),
            "replay_artifact": str(replay_artifact),
            **_agent_task_ids(result),
        }
    )
    store.save_task(agent_task)
    return response


def _execute_selected_workflow(
    config: ProjectConfig,
    store: RunStore,
    retrieval: RetrievalService,
    workflow_type: str,
    *,
    tool: str | None,
    files: list[str],
    extra_args: list[str],
    apply_patch: bool,
    logs: list[Path],
    waveforms: list[Path],
    report: Path | None,
    exclusions: Path | None,
    formal_run_id: str | None,
    rtl: list[Path],
    spec: list[Path],
    dut: Path | None,
    output: Path | None,
    output_dir: Path | None,
    provider_name: str | None,
    intent: str,
) -> dict[str, object]:
    if workflow_type == "compile_repair":
        return _execute_agent_repair(config, store, retrieval, tool=tool, files=files, extra_args=extra_args, apply_patch=apply_patch)
    if workflow_type == "triage":
        if not logs:
            raise ValueError("agent triage requires at least one --logs path")
        run, clusters, context = triage_logs(config, store, retrieval, logs, waveform_paths=waveforms or None)
        payload = {
            "run_id": run.run_id,
            "cluster_count": len(clusters),
            "context_id": context.context_id,
            "waveform_count": int(run.inputs.get("waveform_count", 0)),
            "clusters": [dataclass_to_dict(cluster) for cluster in clusters],
        }
        return {
            "payload": payload,
            "steps": [{"step": "triage_logs", "status": run.status, "run_id": run.run_id, "cluster_count": len(clusters)}],
            "evidence": {"run_ids": [run.run_id], "context_ids": [context.context_id]},
        }
    if workflow_type == "coverage_plan":
        if report is None:
            raise ValueError("agent coverage planning requires --report")
        plan, run, context = execute_coverage_plan(
            config,
            store,
            retrieval,
            report,
            exclusions_path=exclusions,
            formal_run_id=formal_run_id,
            rtl_paths=rtl,
            spec_paths=spec,
        )
        payload = {
            "run_id": run.run_id,
            "context_id": context.context_id,
            "plan_id": plan.plan_id,
            "report_path": plan.report_path,
            "recommendation_count": len(plan.recommendations),
            "recommendations": [dataclass_to_dict(item) for item in plan.recommendations],
        }
        return {
            "payload": payload,
            "steps": [{"step": "coverage_plan", "status": run.status, "run_id": run.run_id, "recommendation_count": len(plan.recommendations)}],
            "evidence": {"run_ids": [run.run_id], "context_ids": [context.context_id], "plan_id": plan.plan_id},
        }
    if workflow_type == "spec_to_sva":
        if not spec or not rtl:
            raise ValueError("agent SVA generation requires --spec and --rtl")
        provider = build_generation_provider(config, provider_name=provider_name)
        candidate, validation_run, context = execute_generation(config, store, retrieval, provider, spec[0], rtl[0], output_path=output)
        payload = {
            "context_id": context.context_id,
            "candidate_id": candidate.candidate_id if candidate else None,
            "provider": candidate.provider if candidate else getattr(provider, "name", ""),
            "status": candidate.status if candidate else "no_generation",
            "artifact_path": candidate.file_path if candidate else None,
            "validation_run_id": validation_run.run_id if validation_run else None,
            "validation_status": validation_run.status if validation_run else None,
            "validation_summary": validation_run.summary if validation_run else None,
        }
        return {
            "payload": payload,
            "steps": [
                {"step": "generate_candidate", "status": payload["status"], "candidate_id": payload["candidate_id"]},
                {"step": "validate_candidate", "status": payload["validation_status"], "run_id": payload["validation_run_id"]},
            ],
            "evidence": {
                "context_ids": [context.context_id],
                "candidate_id": payload["candidate_id"],
                "validation_run_id": payload["validation_run_id"],
                "artifact_path": payload["artifact_path"],
                "replay_artifacts": candidate.replay_artifacts if candidate else {},
            },
        }
    if workflow_type == "dut_to_cocotb":
        if dut is None:
            raise ValueError("agent cocotb generation requires --dut")
        provider = build_generation_provider(config, provider_name=provider_name)
        spec_path = spec[0] if spec else None
        candidate, run, validation_run, context = execute_cocotb_generation(
            config,
            store,
            retrieval,
            provider,
            dut,
            spec_path=spec_path,
            output_dir=output_dir,
            intent=intent,
        )
        payload = {
            "context_id": context.context_id,
            "run_id": run.run_id if run else None,
            "candidate_id": candidate.candidate_id if candidate else None,
            "provider": candidate.provider if candidate else getattr(provider, "name", ""),
            "status": candidate.status if candidate else "no_generation",
            "artifact_path": candidate.file_path if candidate else None,
            "manifest_path": candidate.manifest_path if candidate else None,
            "validation_run_id": validation_run.run_id if validation_run else None,
            "validation_status": validation_run.status if validation_run else None,
            "validation_summary": validation_run.summary if validation_run else None,
        }
        return {
            "payload": payload,
            "steps": [
                {"step": "generate_candidate", "status": payload["status"], "candidate_id": payload["candidate_id"]},
                {"step": "validate_candidate", "status": payload["validation_status"], "run_id": payload["validation_run_id"]},
            ],
            "evidence": {
                "context_ids": [context.context_id],
                "run_ids": [run.run_id] if run else [],
                "candidate_id": payload["candidate_id"],
                "validation_run_id": payload["validation_run_id"],
                "artifact_path": payload["artifact_path"],
                "manifest_path": payload["manifest_path"],
                "replay_artifacts": candidate.replay_artifacts if candidate else {},
            },
        }
    raise ValueError(f"unsupported agent workflow: {workflow_type}")


def _execute_agent_repair(
    config: ProjectConfig,
    store: RunStore,
    retrieval: RetrievalService,
    *,
    tool: str | None,
    files: list[str],
    extra_args: list[str],
    apply_patch: bool,
) -> dict[str, object]:
    if not tool:
        raise ValueError("agent repair requires --tool")
    if not files:
        raise ValueError("agent repair requires at least one --file")
    adapter = AdapterRegistry().get(tool)
    run_id = stable_id("run", config.project.project_id, "agent", tool, utc_now(), ",".join(files))
    execution = adapter.run(run_id, config.project_root, files, config.project_root / config.artifacts_dir, extra_args=extra_args)
    store.save_observations(execution.observations)
    base_run = VerificationRun(
        run_id=run_id,
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="compile_repair",
        tool=adapter.tool_reference,
        inputs={"files": files, "project_root": str(config.project_root), "extra_args": extra_args, "tool_name": tool},
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
    payload = {
        "run_id": base_run.run_id,
        "status": base_run.status,
        "context_id": context.context_id,
        "patch_id": proposal.patch_id if proposal else None,
        "provider": proposal.provider if proposal else getattr(provider, "name", ""),
        "proposal_explanation": proposal.explanation if proposal else None,
        "evidence_paths": proposal.evidence_paths if proposal else [],
        "replay_artifacts": proposal.replay_artifacts if proposal else {},
        "validation_run_id": validation_run.run_id if validation_run else None,
        "validation_status": validation_run.status if validation_run else None,
        "validation_summary": validation_run.summary if validation_run else None,
    }
    return {
        "payload": payload,
        "steps": [
            {
                "step": "run_adapter_check",
                "status": base_run.status,
                "run_id": base_run.run_id,
                "observation_count": len(base_run.observation_ids),
            },
            {
                "step": "generate_or_repair_candidate",
                "status": "proposed" if proposal else "no_patch",
                "patch_id": payload["patch_id"],
                "provider": payload["provider"],
            },
            {
                "step": "validate_candidate",
                "status": payload["validation_status"],
                "run_id": payload["validation_run_id"],
                "summary": payload["validation_summary"],
            },
        ],
        "evidence": {
            "run_ids": [base_run.run_id],
            "context_ids": [context.context_id],
            "patch_id": payload["patch_id"],
            "validation_run_id": payload["validation_run_id"],
            "replay_artifacts": payload["replay_artifacts"],
        },
    }


def _select_workflow(
    task: str,
    *,
    tool: str | None,
    files: list[str],
    logs: list[Path],
    report: Path | None,
    rtl: list[Path],
    spec: list[Path],
    dut: Path | None,
) -> str:
    lowered = task.lower()
    if tool or files or any(keyword in lowered for keyword in ("repair", "fix", "patch")):
        return "compile_repair"
    if logs or "triage" in lowered or "regression" in lowered:
        return "triage"
    if report or "coverage" in lowered:
        return "coverage_plan"
    if dut or "cocotb" in lowered or "testbench" in lowered:
        return "dut_to_cocotb"
    if (spec and rtl) or "sva" in lowered or "assert" in lowered:
        return "spec_to_sva"
    raise ValueError("could not infer an agent workflow; add --tool/--file, --logs, --report, --dut, or --spec/--rtl")


def _build_plan(workflow_type: str, *, review_required: bool) -> dict[str, object]:
    workflow_steps = {
        "compile_repair": ["run_adapter_check", "generate_or_repair_candidate", "validate_candidate", "report_evidence"],
        "triage": ["triage_logs", "cluster_failures", "report_evidence"],
        "coverage_plan": ["parse_coverage", "retrieve_evidence", "draft_recommendations", "report_evidence"],
        "spec_to_sva": ["generate_candidate", "validate_candidate", "report_evidence"],
        "dut_to_cocotb": ["generate_candidate", "validate_candidate", "report_evidence"],
    }
    return {
        "planner": "builtin_hardware_agent",
        "workflow_type": workflow_type,
        "review_required": review_required,
        "steps": ["retrieve_context", *workflow_steps[workflow_type]],
    }


def _agent_retrieval_query(
    task: str,
    *,
    files: list[str],
    logs: list[Path],
    report: Path | None,
    rtl: list[Path],
    spec: list[Path],
    dut: Path | None,
) -> str:
    terms = [task, *files, *[path.stem for path in logs], *(path.stem for path in rtl), *(path.stem for path in spec)]
    if report:
        terms.append(report.stem)
    if dut:
        terms.append(dut.stem)
    return " ".join(term for term in terms if term)


def _focus_paths(
    config: ProjectConfig,
    *,
    files: list[str],
    logs: list[Path],
    report: Path | None,
    rtl: list[Path],
    spec: list[Path],
    dut: Path | None,
) -> list[str]:
    paths = list(files)
    paths.extend(relative_to(path, config.project_root) for path in logs)
    paths.extend(relative_to(path, config.project_root) for path in rtl)
    paths.extend(relative_to(path, config.project_root) for path in spec)
    if report:
        paths.append(relative_to(report, config.project_root))
    if dut:
        paths.append(relative_to(dut, config.project_root))
    return [path for path in paths if path]


def _agent_status(result: dict[str, object], *, apply_patch: bool) -> str:
    payload = result["payload"]
    if not isinstance(payload, dict):
        return "failed"
    validation_status = payload.get("validation_status")
    if validation_status == "passed":
        return "applied" if apply_patch else "review_required"
    status = payload.get("status")
    if status in {"passed", "planned", "validated"}:
        return str(status)
    if status in {"failed", "rejected"} or validation_status == "failed":
        return "failed"
    if payload.get("patch_id") is None and payload.get("candidate_id") is None and validation_status is None:
        return "no_candidate"
    return str(status or "completed")


def _review_gate_summary(workflow_type: str, status: str, apply_patch: bool) -> str:
    if workflow_type == "compile_repair":
        if apply_patch and status == "applied":
            return "validated patch was applied because --apply was set"
        return "validated patch is saved for human review; rerun repair with --apply or inspect the patch artifact"
    if workflow_type in {"spec_to_sva", "dut_to_cocotb"}:
        return "generated artifact is saved under the configured artifacts directory for review"
    return "workflow is evidence-reporting only"


def _agent_task_ids(result: dict[str, object]) -> dict[str, object]:
    evidence = result.get("evidence", {})
    if not isinstance(evidence, dict):
        return {}
    return {
        "run_ids": evidence.get("run_ids", []),
        "context_ids": evidence.get("context_ids", []),
        "patch_id": evidence.get("patch_id"),
        "candidate_id": evidence.get("candidate_id"),
        "validation_run_id": evidence.get("validation_run_id"),
    }
