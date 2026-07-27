from __future__ import annotations

import subprocess
import sys
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from telchines.adapters.parsing import parse_common_output
from telchines.ci_importers import import_ci_runs
from telchines.config import ProjectConfig
from telchines.coverage_import import import_coverage_report
from telchines.errors import ProviderError
from telchines.models import BenchmarkCase, ToolReference, VerificationRun
from telchines.providers import build_generation_provider, build_repair_provider
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import copy_tree_to_temp, read_json, remove_tree, stable_id, utc_now
from telchines.workflows.agent import execute_agent
from telchines.workflows.coverage import execute_coverage_plan
from telchines.workflows.gen_cocotb import execute_cocotb_generation
from telchines.workflows.gen_sva import execute_generation
from telchines.workflows.repair import execute_repair
from telchines.workflows.triage import triage_logs


def load_benchmark_cases(root: Path) -> list[BenchmarkCase]:
    cases: list[BenchmarkCase] = []
    for path in sorted(root.glob("*.json")):
        cases.append(BenchmarkCase(**read_json(path)))
    return cases


def run_default_suite(config: ProjectConfig, store: RunStore) -> dict[str, object]:
    local_benchmarks_root = config.project_root / "benchmarks"
    benchmarks_root = local_benchmarks_root if _has_benchmark_cases(local_benchmarks_root) else _bundled_benchmarks_root()
    temp_project_root = Path(tempfile.mkdtemp(prefix="telchines-eval-"))
    try:
        # Benchmarks may use deterministic local-command providers to exercise
        # copied fixture tools.  Run all cases in a disposable project so this
        # narrow policy exception cannot change the caller's project policy.
        shutil.copytree(benchmarks_root, temp_project_root / "benchmarks")
        temp_config = ProjectConfig.init_project(temp_project_root, name=f"{config.project.name}-eval")
        temp_config.project.project_id = config.project.project_id
        temp_config.project.model_policy = deepcopy(config.project.model_policy)
        temp_config.model_mode = config.model_mode
        temp_config.no_egress = config.no_egress
        temp_config.allow_local_commands = True
        temp_config.retrieval = deepcopy(config.retrieval)
        temp_config.generation = deepcopy(config.generation)
        temp_config.adapters = list(config.adapters)
        temp_config.save()
        report = _run_default_suite(temp_config, RunStore(temp_config), isolated_fixture_commands=True)
        store.save_report("latest_eval", report)
        return report
    finally:
        remove_tree(temp_project_root)


def _run_default_suite(
    config: ProjectConfig,
    store: RunStore,
    *,
    isolated_fixture_commands: bool = False,
) -> dict[str, object]:
    benchmarks_root = config.project_root / "benchmarks"
    cases = load_benchmark_cases(benchmarks_root)
    retrieval = RetrievalService(config)
    retrieval.build_index()
    results: list[dict[str, object]] = []
    for case in cases:
        if case.task_type == "agent":
            results.append(_run_agent_case(config, store, retrieval, case))
        elif case.task_type == "repair":
            results.append(_run_repair_case(config, store, retrieval, case))
        elif case.task_type == "triage":
            results.append(_run_triage_case(config, store, retrieval, case))
        elif case.task_type == "retrieval":
            results.append(_run_retrieval_case(config, case))
        elif case.task_type == "sva":
            results.append(_run_sva_case(config, case))
        elif case.task_type == "cocotb":
            results.append(_run_cocotb_case(config, case))
        elif case.task_type == "coverage":
            results.append(_run_coverage_case(config, case))
        elif case.task_type == "import":
            results.append(_run_import_case(config, case))
        elif case.task_type == "coverage_import":
            results.append(_run_coverage_import_case(config, case))
        elif case.task_type == "provider_response":
            results.append(_run_provider_response_case(config, case))
    passed = sum(1 for result in results if result["passed"])
    warning_count = sum(1 for result in results if result.get("benchmark_status") == "passed_with_warnings")
    report = {
        "suite": "default",
        "ran_at": utc_now(),
        "cases": results,
        "status": "failed" if passed != len(results) else ("passed_with_warnings" if warning_count else "passed"),
        "passed": passed,
        "total": len(results),
        "warning_count": warning_count,
        "metrics": _aggregate_metrics(results),
    }
    if isolated_fixture_commands:
        report["evaluation_environment"] = {
            "type": "disposable_benchmark_project",
            "fixture_local_commands_enabled": True,
            "policy_scope": "copied benchmark fixtures only",
        }
    store.save_report("latest_eval", report)
    return report


def _has_benchmark_cases(root: Path) -> bool:
    return root.exists() and any(root.glob("*.json"))


def _bundled_benchmarks_root() -> Path:
    root = Path(__file__).resolve().parent / "benchmarks"
    if not _has_benchmark_cases(root):
        raise FileNotFoundError("bundled benchmark suite is missing from the installed package")
    return root


def _init_fixture_project(root: Path, source_config: ProjectConfig) -> ProjectConfig:
    """Initialize a copied fixture with the evaluation project's runtime policy."""
    config = ProjectConfig.init_project(root)
    config.project.project_id = source_config.project.project_id
    config.project.model_policy = deepcopy(source_config.project.model_policy)
    config.model_mode = source_config.model_mode
    config.no_egress = source_config.no_egress
    config.allow_local_commands = source_config.allow_local_commands
    config.retrieval = deepcopy(source_config.retrieval)
    config.generation = deepcopy(source_config.generation)
    config.adapters = list(source_config.adapters)
    config.save()
    return config


def _run_repair_case(config: ProjectConfig, store: RunStore, retrieval: RetrievalService, case: BenchmarkCase) -> dict[str, object]:
    fixture_root = config.project_root / case.fixture_root
    temp_root = copy_tree_to_temp(fixture_root)
    try:
        temp_config = _init_fixture_project(temp_root, config)
        model_policy = _resolve_runtime_placeholders(deepcopy(case.config.get("model_policy", {})))
        if model_policy:
            temp_config.project.model_policy = model_policy
        temp_config.save()
        temp_store = RunStore(temp_config)
        temp_retrieval = RetrievalService(temp_config)
        temp_retrieval.build_index()
        command = [sys.executable, *case.config["validator_command"]]
        process = subprocess.run(command, cwd=temp_root, capture_output=True, text=True, check=False)
        run_id = stable_id("run", case.benchmark_id, "initial")
        observations = parse_common_output(run_id, process.stdout + process.stderr)
        temp_store.save_observations(observations)
        base_run = VerificationRun(
            run_id=run_id,
            project_id=temp_config.project.project_id,
            commit_sha="benchmark",
            workflow_type="compile_repair",
            tool=ToolReference(kind="fixture", name="fixture_lint", version="0.1"),
            inputs={"files": [case.config["target_file"]], "project_root": str(temp_root)},
            status="failed" if process.returncode else "passed",
            started_at=utc_now(),
            finished_at=utc_now(),
            exit_code=process.returncode,
            artifacts={},
            observation_ids=[observation.observation_id for observation in observations],
            summary=case.title,
            replay_command=command,
        )
        temp_store.save_run(base_run)
        provider = build_repair_provider(temp_config)
        proposal, validation_run, _ = execute_repair(temp_config, temp_store, temp_retrieval, provider, base_run, apply_patch=False)
        expected = str(case.scoring.get("expected", "pass"))
        if expected == "no_patch":
            passed = proposal is None and validation_run is None
        else:
            passed = validation_run is not None and validation_run.status == "passed"
        validation_status = validation_run.status if validation_run else "not_run"
        validation_mode = str(validation_run.tool_result.get("validation_mode", "fixture_tool")) if validation_run else "not_run"
        return {
            "benchmark_id": case.benchmark_id,
            "task_type": case.task_type,
            "passed": passed,
            "benchmark_scope": _case_scope(case),
            "execution_backing": _case_execution_backing(case, "fixture_tool"),
            "patch_id": proposal.patch_id if proposal else None,
            "validation_status": validation_status,
            "validation_mode": validation_mode,
        }
    finally:
        remove_tree(temp_root)


def _run_agent_case(config: ProjectConfig, store: RunStore, retrieval: RetrievalService, case: BenchmarkCase) -> dict[str, object]:
    fixture_root = config.project_root / case.fixture_root
    temp_root = copy_tree_to_temp(fixture_root)
    try:
        temp_config = _init_fixture_project(temp_root, config)
        model_policy = _resolve_runtime_placeholders(deepcopy(case.config.get("model_policy", {})))
        if model_policy:
            temp_config.project.model_policy = model_policy
        temp_config.save()
        temp_store = RunStore(temp_config)
        temp_retrieval = RetrievalService(temp_config)
        temp_retrieval.build_index()
        payload = execute_agent(
            temp_config,
            temp_store,
            temp_retrieval,
            str(case.config["task"]),
            tool=str(case.config.get("tool")) if case.config.get("tool") else None,
            files=[str(path) for path in case.config.get("files", [])],
            extra_args=[str(arg) for arg in case.config.get("extra_args", [])],
            apply_patch=bool(case.config.get("apply_patch", False)),
            logs=[temp_root / str(path) for path in case.config.get("logs", [])],
            waveforms=[temp_root / str(path) for path in case.config.get("waveforms", [])],
            report=(temp_root / str(case.config["report"])) if case.config.get("report") else None,
            rtl=[temp_root / str(path) for path in case.config.get("rtl", [])],
            spec=[temp_root / str(path) for path in case.config.get("spec", [])],
            dut=(temp_root / str(case.config["dut"])) if case.config.get("dut") else None,
        )
        step_names = [str(step.get("step")) for step in payload.get("steps", []) if isinstance(step, dict)]
        evidence = payload.get("evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
        replay_artifacts = payload.get("replay_artifacts", {})
        if not isinstance(replay_artifacts, dict):
            replay_artifacts = {}
        required_steps = [str(step) for step in case.scoring.get("required_steps", [])]
        required_evidence = [str(item) for item in case.scoring.get("required_evidence", [])]
        passed = (
            payload.get("workflow_type") == case.scoring.get("expected_workflow_type")
            and payload.get("status") == case.scoring.get("expected_status", payload.get("status"))
            and all(step in step_names for step in required_steps)
            and all(evidence.get(item) for item in required_evidence)
            and bool(replay_artifacts.get("replay_artifact"))
        )
        return {
            "benchmark_id": case.benchmark_id,
            "task_type": case.task_type,
            "passed": passed,
            "benchmark_scope": _case_scope(case),
            "execution_backing": _case_execution_backing(case, "fixture_tool"),
            "workflow_type": payload.get("workflow_type"),
            "status": payload.get("status"),
            "context_id": payload.get("context_id"),
            "step_names": step_names,
            "patch_id": evidence.get("patch_id"),
            "validation_run_id": evidence.get("validation_run_id"),
            "replay_artifact": replay_artifacts.get("replay_artifact"),
        }
    finally:
        remove_tree(temp_root)


def _run_triage_case(config: ProjectConfig, store: RunStore, retrieval: RetrievalService, case: BenchmarkCase) -> dict[str, object]:
    logs_path = config.project_root / case.fixture_root / case.config["logs_path"]
    run, clusters, _ = triage_logs(config, store, retrieval, logs_path)
    expected = int(case.scoring.get("min_clusters", 1))
    expected_signatures = {str(value) for value in case.scoring.get("expected_signatures", [])}
    signatures = {cluster.signature for cluster in clusters}
    passed = len(clusters) >= expected and expected_signatures.issubset(signatures)
    return {
        "benchmark_id": case.benchmark_id,
        "task_type": case.task_type,
        "passed": passed,
        "benchmark_scope": _case_scope(case),
        "execution_backing": _case_execution_backing(case, "fixture_logs"),
        "run_id": run.run_id,
        "cluster_count": len(clusters),
        "matched_signatures": sorted(expected_signatures & signatures),
        "evidence_hits": sum(len(cluster.evidence_hits) for cluster in clusters),
    }


def _run_retrieval_case(config: ProjectConfig, case: BenchmarkCase) -> dict[str, object]:
    fixture_root = config.project_root / case.fixture_root
    temp_root = copy_tree_to_temp(fixture_root)
    try:
        temp_config = _init_fixture_project(temp_root, config)
        retrieval_overrides = case.config.get("retrieval", {})
        if isinstance(retrieval_overrides, dict) and retrieval_overrides:
            temp_config.retrieval.update(retrieval_overrides)
            temp_config.save()
        retrieval = RetrievalService(temp_config)
        retrieval.build_index()
        context = retrieval.search(
            case.config["query"],
            mode=str(case.config.get("mode", "general")),
            focus_paths=list(case.config.get("focus_paths", [])),
            limit=int(case.config.get("limit", temp_config.retrieval.get("max_hits", 5))),
        )
        expected_paths = {path.replace("\\", "/") for path in case.scoring.get("expected_paths", [])}
        hit_paths = [hit.path for hit in context.hits]
        matched_paths = sorted(path for path in expected_paths if any(hit_path.endswith(path) for hit_path in hit_paths))
        recall = len(matched_paths) / max(len(expected_paths), 1)
        citation_coverage = sum(1 for hit in context.hits if hit.citation) / max(len(context.hits), 1)
        expected_external = {path.replace("\\", "/") for path in case.scoring.get("expected_external_paths", [])}
        matched_external = sorted(path for path in expected_external if any(hit_path.endswith(path) for hit_path in hit_paths))
        external_recall = len(matched_external) / max(len(expected_external), 1) if expected_external else 1.0
        min_recall = float(case.scoring.get("min_recall", 1.0))
        min_citation_coverage = float(case.scoring.get("min_citation_coverage", 1.0))
        min_external_recall = float(case.scoring.get("min_external_recall", 1.0)) if expected_external else 1.0
        passed = recall >= min_recall and citation_coverage >= min_citation_coverage and external_recall >= min_external_recall
        return {
            "benchmark_id": case.benchmark_id,
            "task_type": case.task_type,
            "passed": passed,
            "benchmark_scope": _case_scope(case),
            "execution_backing": _case_execution_backing(case, "fixture_retrieval"),
            "mode": context.mode,
            "query": context.query,
            "hit_count": len(context.hits),
            "matched_paths": matched_paths,
            "matched_external_paths": matched_external,
            "recall_at_k": round(recall, 3),
            "external_recall_at_k": round(external_recall, 3),
            "citation_coverage": round(citation_coverage, 3),
        }
    finally:
        remove_tree(temp_root)


def _run_sva_case(config: ProjectConfig, case: BenchmarkCase) -> dict[str, object]:
    fixture_root = config.project_root / case.fixture_root
    temp_root = copy_tree_to_temp(fixture_root)
    try:
        temp_config = _init_fixture_project(temp_root, config)
        model_policy = _resolve_runtime_placeholders(deepcopy(case.config.get("model_policy", {})))
        if model_policy:
            temp_config.project.model_policy = model_policy
            temp_config.save()
        temp_store = RunStore(temp_config)
        retrieval = RetrievalService(temp_config)
        retrieval.build_index()
        provider_name = case.config.get("provider")
        provider = build_generation_provider(temp_config, provider_name=str(provider_name) if provider_name else None)
        candidate, validation_run, context = execute_generation(
            temp_config,
            temp_store,
            retrieval,
            provider,
            temp_root / case.config["spec_path"],
            temp_root / case.config["rtl_path"],
        )
        expected_validation = str(case.scoring.get("expected_validation", "passed"))
        min_properties = int(case.scoring.get("min_properties", 1))
        expected_names = {str(name) for name in case.scoring.get("expected_property_names", [])}
        expected_citations = {str(item).replace("\\", "/") for item in case.scoring.get("expected_citations", [])}
        property_names = {item.name for item in candidate.properties} if candidate else set()
        property_citations = (
            {item.source_citation.replace("\\", "/") for item in candidate.properties if item.source_citation}
            | {path.replace("\\", "/") for path in candidate.evidence_paths}
            if candidate
            else set()
        )
        matched_names = sorted(expected_names & property_names)
        matched_citations = sorted(expected_citations & property_citations)
        property_name_match_rate = len(matched_names) / max(len(expected_names), 1)
        citation_match_rate = len(matched_citations) / max(len(expected_citations), 1)
        property_count = len(candidate.properties) if candidate else 0
        generated_candidate = candidate is not None
        artifact_exists = bool(candidate and (temp_root / candidate.file_path).exists())
        validation_status = validation_run.status if validation_run else "not_run"
        validation_mode = str(validation_run.tool_result.get("validation_mode", "")) if validation_run else "not_run"
        formal_status = str(validation_run.tool_result.get("formal_status", "not_run")) if validation_run else "not_run"
        structural_status = str(validation_run.tool_result.get("structural_status", "not_run")) if validation_run else "not_run"
        syntax_status = str(validation_run.tool_result.get("syntax_status", "not_run")) if validation_run else "not_run"
        adapter_status = str(validation_run.tool_result.get("adapter_status", "not_run")) if validation_run else "not_run"
        proof_status = str(validation_run.tool_result.get("proof_status", "not_attempted")) if validation_run else "not_attempted"
        overall_status = str(validation_run.tool_result.get("overall_status", validation_status)) if validation_run else "not_run"
        passed = (
            generated_candidate
            and artifact_exists
            and property_count >= min_properties
            and validation_status == expected_validation
            and property_name_match_rate >= float(case.scoring.get("min_property_name_match_rate", 1.0))
            and citation_match_rate >= float(case.scoring.get("min_citation_match_rate", 1.0))
        )
        return {
            "benchmark_id": case.benchmark_id,
            "task_type": case.task_type,
            "passed": passed,
            "benchmark_status": _benchmark_status(passed, overall_status),
            "benchmark_scope": _case_scope(case),
            "execution_backing": _case_execution_backing(case, _validation_backing(validation_mode, formal_status=formal_status)),
            "provider": provider.name,
            "context_id": context.context_id,
            "generated_candidate": generated_candidate,
            "candidate_id": candidate.candidate_id if candidate else None,
            "validation_run_id": validation_run.run_id if validation_run else None,
            "validation_status": validation_status,
            "validation_mode": validation_mode,
            "structural_status": structural_status,
            "syntax_status": syntax_status,
            "adapter_status": adapter_status,
            "formal_status": formal_status,
            "proof_status": proof_status,
            "overall_status": overall_status,
            "artifact_generated": artifact_exists,
            "artifact_path": candidate.file_path if candidate else None,
            "property_count": property_count,
            "matched_property_names": matched_names,
            "matched_citations": matched_citations,
            "property_name_match_rate": round(property_name_match_rate, 3),
            "citation_match_rate": round(citation_match_rate, 3),
        }
    finally:
        remove_tree(temp_root)


def _run_cocotb_case(config: ProjectConfig, case: BenchmarkCase) -> dict[str, object]:
    fixture_root = config.project_root / case.fixture_root
    temp_root = copy_tree_to_temp(fixture_root)
    try:
        temp_config = _init_fixture_project(temp_root, config)
        model_policy = _resolve_runtime_placeholders(deepcopy(case.config.get("model_policy", {})))
        if model_policy:
            temp_config.project.model_policy = model_policy
            temp_config.save()
        temp_store = RunStore(temp_config)
        retrieval = RetrievalService(temp_config)
        retrieval.build_index()
        provider_name = case.config.get("provider")
        provider = build_generation_provider(temp_config, provider_name=str(provider_name) if provider_name else None)
        candidate, run, validation_run, context = execute_cocotb_generation(
            temp_config,
            temp_store,
            retrieval,
            provider,
            temp_root / case.config["dut_path"],
            spec_path=(temp_root / case.config["spec_path"]) if case.config.get("spec_path") else None,
            output_dir=(temp_root / case.config["output_dir"]) if case.config.get("output_dir") else None,
            intent=str(case.config.get("intent", "")),
        )
        expected_validation = str(case.scoring.get("expected_validation", "passed"))
        expected_identifiers = {str(name) for name in case.scoring.get("expected_identifiers", [])}
        matched_identifiers = sorted(
            name for name in expected_identifiers if candidate and (f"dut.{name}" in candidate.candidate_content or f"\"{name}\"" in candidate.candidate_content)
        )
        identifier_match_rate = len(matched_identifiers) / max(len(expected_identifiers), 1)
        assumption_count = len(candidate.assumptions) if candidate else 0
        validation_status = validation_run.status if validation_run else "not_run"
        validation_mode = str(validation_run.tool_result.get("validation_mode", "")) if validation_run else "not_run"
        executable_status = str(validation_run.tool_result.get("executable_status", "not_run")) if validation_run else "not_run"
        executable_contract = str(validation_run.tool_result.get("executable_contract", "not_evaluated")) if validation_run else "not_evaluated"
        executable_expectation = str(case.scoring.get("expected_executable", "ignore"))
        artifact_exists = bool(candidate and (temp_root / candidate.file_path).exists())
        manifest_exists = bool(candidate and (temp_root / candidate.manifest_path).exists())
        passed = (
            candidate is not None
            and run is not None
            and validation_status == expected_validation
            and artifact_exists
            and manifest_exists
            and assumption_count >= int(case.scoring.get("min_assumptions", 1))
            and identifier_match_rate >= float(case.scoring.get("min_identifier_match_rate", 1.0))
            and _cocotb_executable_expectation_met(executable_expectation, executable_contract, executable_status)
        )
        return {
            "benchmark_id": case.benchmark_id,
            "task_type": case.task_type,
            "passed": passed,
            "benchmark_scope": _case_scope(case),
            "execution_backing": _case_execution_backing(case, _validation_backing(validation_mode, executable_status=executable_status)),
            "provider": provider.name,
            "context_id": context.context_id,
            "run_id": run.run_id if run else None,
            "candidate_id": candidate.candidate_id if candidate else None,
            "validation_run_id": validation_run.run_id if validation_run else None,
            "validation_status": validation_status,
            "validation_mode": validation_mode,
            "executable_status": executable_status,
            "executable_contract": executable_contract,
            "executable_expectation": executable_expectation,
            "artifact_generated": artifact_exists,
            "manifest_generated": manifest_exists,
            "artifact_path": candidate.file_path if candidate else None,
            "manifest_path": candidate.manifest_path if candidate else None,
            "assumption_count": assumption_count,
            "matched_identifiers": matched_identifiers,
            "identifier_match_rate": round(identifier_match_rate, 3),
        }
    finally:
        remove_tree(temp_root)


def _run_coverage_case(config: ProjectConfig, case: BenchmarkCase) -> dict[str, object]:
    fixture_root = config.project_root / case.fixture_root
    temp_root = copy_tree_to_temp(fixture_root)
    try:
        temp_config = _init_fixture_project(temp_root, config)
        temp_store = RunStore(temp_config)
        retrieval = RetrievalService(temp_config)
        retrieval.build_index()
        formal_run_id = None
        formal_seed = case.config.get("formal_run")
        if isinstance(formal_seed, dict):
            formal_run_id = str(formal_seed.get("run_id", "formal_seed"))
            formal_run = VerificationRun(
                run_id=formal_run_id,
                project_id=temp_config.project.project_id,
                commit_sha="benchmark",
                workflow_type=str(formal_seed.get("workflow_type", "formal_validation")),
                tool=ToolReference(kind="formal", name=str(formal_seed.get("tool_name", "symbiyosys")), version="0.1"),
                inputs={},
                status=str(formal_seed.get("status", "passed")),
                started_at=utc_now(),
                finished_at=utc_now(),
                exit_code=0,
                artifacts=dict(formal_seed.get("artifacts", {})),
                tool_result=dict(formal_seed.get("tool_result", {})),
                summary=str(formal_seed.get("summary", "")),
            )
            temp_store.save_run(formal_run)
        plan, run, context = execute_coverage_plan(
            temp_config,
            temp_store,
            retrieval,
            temp_root / case.config["report_path"],
            exclusions_path=(temp_root / case.config["exclusions_path"]) if case.config.get("exclusions_path") else None,
            formal_run_id=formal_run_id,
            rtl_paths=[temp_root / value for value in case.config.get("rtl_paths", [])],
            spec_paths=[temp_root / value for value in case.config.get("spec_paths", [])],
        )
        expected_classifications = {str(value) for value in case.scoring.get("expected_classifications", [])}
        expected_item_ids = {str(value) for value in case.scoring.get("expected_item_ids", [])}
        actual_classifications = [item.classification for item in plan.recommendations]
        actual_item_ids = [item.item_id for item in plan.recommendations]
        matched_classifications = sorted(expected_classifications & set(actual_classifications))
        matched_item_ids = sorted(expected_item_ids & set(actual_item_ids))
        evidence_count = sum(len(item.evidence_citations) for item in plan.recommendations[: int(case.scoring.get("inspect_recommendations", 3))])
        top_supporting_runs = {run_id for item in plan.recommendations[:3] for run_id in item.supporting_run_ids}
        expected_supporting_runs = {str(value) for value in case.scoring.get("expected_supporting_runs", [])}
        passed = (
            run is not None
            and len(plan.recommendations) >= int(case.scoring.get("min_recommendations", 1))
            and len(matched_classifications) >= int(case.scoring.get("min_classification_matches", len(expected_classifications)))
            and len(matched_item_ids) >= int(case.scoring.get("min_item_matches", len(expected_item_ids)))
            and evidence_count >= int(case.scoring.get("min_evidence_citations", 1))
            and expected_supporting_runs.issubset(top_supporting_runs)
        )
        return {
            "benchmark_id": case.benchmark_id,
            "task_type": case.task_type,
            "passed": passed,
            "benchmark_scope": _case_scope(case),
            "execution_backing": _case_execution_backing(case, "fixture_structure"),
            "context_id": context.context_id,
            "run_id": run.run_id,
            "plan_id": plan.plan_id,
            "recommendation_count": len(plan.recommendations),
            "matched_classifications": matched_classifications,
            "matched_item_ids": matched_item_ids,
            "evidence_count": evidence_count,
            "supporting_run_count": len(top_supporting_runs),
        }
    finally:
        remove_tree(temp_root)


def _run_import_case(config: ProjectConfig, case: BenchmarkCase) -> dict[str, object]:
    fixture_root = config.project_root / case.fixture_root
    temp_root = copy_tree_to_temp(fixture_root)
    try:
        temp_config = _init_fixture_project(temp_root, config)
        temp_store = RunStore(temp_config)
        importer = str(case.config["importer"])
        source = temp_root / str(case.config["source_path"])
        payload = import_ci_runs(temp_config, temp_store, source, importer=importer, dry_run=False)
        expected_count = int(case.scoring.get("expected_imported_count", 1))
        expected_observations = int(case.scoring.get("min_observations", 1))
        runs = temp_store.list_runs_by_workflow("regression_import")
        observation_count = sum(len(run.observation_ids) for run in runs)
        tool_names = {run.tool.name for run in runs}
        passed = (
            int(payload["imported_count"]) >= expected_count
            and len(runs) >= expected_count
            and observation_count >= expected_observations
            and importer in tool_names
        )
        return {
            "benchmark_id": case.benchmark_id,
            "task_type": case.task_type,
            "passed": passed,
            "benchmark_scope": _case_scope(case),
            "execution_backing": _case_execution_backing(case, "fixture_import"),
            "source_format": importer,
            "imported_count": payload["imported_count"],
            "stored_count": len(runs),
            "observation_count": observation_count,
        }
    finally:
        remove_tree(temp_root)


def _cocotb_executable_expectation_met(expectation: str, contract: str, status: str) -> bool:
    if expectation == "ignore":
        return True
    if expectation == "when_supported":
        return (contract == "supported" and status == "passed") or (contract == "unsupported" and status == "skipped")
    return status == expectation


def _run_coverage_import_case(config: ProjectConfig, case: BenchmarkCase) -> dict[str, object]:
    fixture_root = config.project_root / case.fixture_root
    temp_root = copy_tree_to_temp(fixture_root)
    try:
        temp_config = _init_fixture_project(temp_root, config)
        temp_store = RunStore(temp_config)
        retrieval = RetrievalService(temp_config)
        retrieval.build_index()
        output = temp_root / str(case.config.get("output_path", "cov/imported_coverage.json"))
        import_payload = import_coverage_report(
            temp_config,
            temp_root / str(case.config["source_path"]),
            source_format=str(case.config["source_format"]),
            output=output,
        )
        plan, run, context = execute_coverage_plan(temp_config, temp_store, retrieval, output)
        expected_classifications = {str(value) for value in case.scoring.get("expected_classifications", [])}
        actual_classifications = {item.classification for item in plan.recommendations}
        passed = (
            int(import_payload["item_count"]) >= int(case.scoring.get("min_items", 1))
            and len(plan.recommendations) >= int(case.scoring.get("min_recommendations", 1))
            and expected_classifications.issubset(actual_classifications)
        )
        return {
            "benchmark_id": case.benchmark_id,
            "task_type": case.task_type,
            "passed": passed,
            "benchmark_scope": _case_scope(case),
            "execution_backing": _case_execution_backing(case, "fixture_import"),
            "context_id": context.context_id,
            "run_id": run.run_id,
            "item_count": import_payload["item_count"],
            "recommendation_count": len(plan.recommendations),
            "matched_classifications": sorted(expected_classifications & actual_classifications),
        }
    finally:
        remove_tree(temp_root)


def _run_provider_response_case(config: ProjectConfig, case: BenchmarkCase) -> dict[str, object]:
    fixture_root = config.project_root / case.fixture_root
    temp_root = copy_tree_to_temp(fixture_root)
    workflow = str(case.config.get("workflow", "sva"))
    try:
        temp_config = _init_fixture_project(temp_root, config)
        model_policy = _resolve_runtime_placeholders(deepcopy(case.config.get("model_policy", {})))
        if model_policy:
            temp_config.project.model_policy = model_policy
            temp_config.save()
        temp_store = RunStore(temp_config)
        retrieval = RetrievalService(temp_config)
        retrieval.build_index()
        provider_name = case.config.get("provider")
        provider = build_generation_provider(temp_config, provider_name=str(provider_name) if provider_name else None)
        error_message = ""
        generated_candidate = False
        validation_status = "not_run"
        validation_mode = "not_run"
        try:
            if workflow == "cocotb":
                candidate, _run, validation_run, _context = execute_cocotb_generation(
                    temp_config,
                    temp_store,
                    retrieval,
                    provider,
                    temp_root / str(case.config["dut_path"]),
                    spec_path=(temp_root / str(case.config["spec_path"])) if case.config.get("spec_path") else None,
                    output_dir=(temp_root / str(case.config["output_dir"])) if case.config.get("output_dir") else None,
                    intent=str(case.config.get("intent", "")),
                )
            else:
                candidate, validation_run, _context = execute_generation(
                    temp_config,
                    temp_store,
                    retrieval,
                    provider,
                    temp_root / str(case.config["spec_path"]),
                    temp_root / str(case.config["rtl_path"]),
                )
            generated_candidate = candidate is not None
            validation_status = validation_run.status if validation_run else "not_run"
            validation_mode = str(validation_run.tool_result.get("validation_mode", "")) if validation_run else "not_run"
            status = "completed"
        except ProviderError as exc:
            status = "provider_error"
            error_message = str(exc)
        expected_status = str(case.scoring.get("expected_status", "provider_error"))
        expected_error = str(case.scoring.get("expected_error_contains", ""))
        passed = status == expected_status and (not expected_error or expected_error in error_message)
        return {
            "benchmark_id": case.benchmark_id,
            "task_type": case.task_type,
            "passed": passed,
            "benchmark_scope": _case_scope(case),
            "execution_backing": _case_execution_backing(case, "fixture_provider_response"),
            "workflow": workflow,
            "provider": provider.name,
            "status": status,
            "error_message": error_message,
            "generated_candidate": generated_candidate,
            "validation_status": validation_status,
            "validation_mode": validation_mode,
        }
    finally:
        remove_tree(temp_root)


def _aggregate_metrics(results: list[dict[str, object]]) -> dict[str, object]:
    agent_results = [result for result in results if result["task_type"] == "agent"]
    retrieval_results = [result for result in results if result["task_type"] == "retrieval"]
    sva_results = [result for result in results if result["task_type"] == "sva"]
    cocotb_results = [result for result in results if result["task_type"] == "cocotb"]
    coverage_results = [result for result in results if result["task_type"] == "coverage"]
    triage_results = [result for result in results if result["task_type"] == "triage"]
    metrics: dict[str, object] = {}
    if agent_results:
        metrics["agent"] = {
            "cases": len(agent_results),
            "pass_rate": round(sum(1 for result in agent_results if bool(result["passed"])) / len(agent_results), 3),
            "review_required_count": sum(1 for result in agent_results if result.get("status") == "review_required"),
        }
    if retrieval_results:
        metrics["retrieval"] = {
            "cases": len(retrieval_results),
            "avg_recall_at_k": round(sum(float(result["recall_at_k"]) for result in retrieval_results) / len(retrieval_results), 3),
            "avg_external_recall_at_k": round(
                sum(float(result["external_recall_at_k"]) for result in retrieval_results) / len(retrieval_results),
                3,
            ),
            "avg_citation_coverage": round(
                sum(float(result["citation_coverage"]) for result in retrieval_results) / len(retrieval_results),
                3,
            ),
        }
    if sva_results:
        metrics["sva"] = {
            "cases": len(sva_results),
            "generation_rate": round(
                sum(1 for result in sva_results if bool(result["generated_candidate"])) / len(sva_results),
                3,
            ),
            "validation_pass_rate": round(
                sum(1 for result in sva_results if result["validation_status"] == "passed") / len(sva_results),
                3,
            ),
            "validation_modes": _count_values(sva_results, "validation_mode"),
            "structural_statuses": _count_values(sva_results, "structural_status"),
            "syntax_statuses": _count_values(sva_results, "syntax_status"),
            "adapter_statuses": _count_values(sva_results, "adapter_status"),
            "formal_statuses": _count_values(sva_results, "formal_status"),
            "proof_statuses": _count_values(sva_results, "proof_status"),
            "overall_statuses": _count_values(sva_results, "overall_status"),
            "formal_failure_count": sum(1 for result in sva_results if result["formal_status"] == "failed"),
            "benchmark_statuses": _count_values(sva_results, "benchmark_status"),
            "artifact_generation_rate": round(
                sum(1 for result in sva_results if bool(result["artifact_generated"])) / len(sva_results),
                3,
            ),
            "avg_property_count": round(
                sum(int(result["property_count"]) for result in sva_results) / len(sva_results),
                3,
            ),
            "avg_property_name_match_rate": round(
                sum(float(result["property_name_match_rate"]) for result in sva_results) / len(sva_results),
                3,
            ),
            "avg_citation_match_rate": round(
                sum(float(result["citation_match_rate"]) for result in sva_results) / len(sva_results),
                3,
            ),
        }
    if cocotb_results:
        metrics["cocotb"] = {
            "cases": len(cocotb_results),
            "generation_rate": round(
                sum(1 for result in cocotb_results if bool(result["artifact_generated"])) / len(cocotb_results),
                3,
            ),
            "validation_pass_rate": round(
                sum(1 for result in cocotb_results if result["validation_status"] == "passed") / len(cocotb_results),
                3,
            ),
            "validation_modes": _count_values(cocotb_results, "validation_mode"),
            "executable_statuses": _count_values(cocotb_results, "executable_status"),
            "manifest_generation_rate": round(
                sum(1 for result in cocotb_results if bool(result["manifest_generated"])) / len(cocotb_results),
                3,
            ),
            "avg_assumption_count": round(
                sum(int(result["assumption_count"]) for result in cocotb_results) / len(cocotb_results),
                3,
            ),
            "avg_identifier_match_rate": round(
                sum(float(result["identifier_match_rate"]) for result in cocotb_results) / len(cocotb_results),
                3,
            ),
        }
    if coverage_results:
        metrics["coverage"] = {
            "cases": len(coverage_results),
            "avg_recommendation_count": round(
                sum(int(result["recommendation_count"]) for result in coverage_results) / len(coverage_results),
                3,
            ),
            "avg_evidence_count": round(
                sum(int(result["evidence_count"]) for result in coverage_results) / len(coverage_results),
                3,
            ),
            "avg_supporting_run_count": round(
                sum(int(result["supporting_run_count"]) for result in coverage_results) / len(coverage_results),
                3,
            ),
        }
    import_results = [result for result in results if result["task_type"] == "import"]
    if import_results:
        metrics["import"] = {
            "cases": len(import_results),
            "avg_imported_count": round(sum(int(result["imported_count"]) for result in import_results) / len(import_results), 3),
            "avg_observation_count": round(sum(int(result["observation_count"]) for result in import_results) / len(import_results), 3),
        }
    coverage_import_results = [result for result in results if result["task_type"] == "coverage_import"]
    if coverage_import_results:
        metrics["coverage_import"] = {
            "cases": len(coverage_import_results),
            "avg_item_count": round(sum(int(result["item_count"]) for result in coverage_import_results) / len(coverage_import_results), 3),
            "avg_recommendation_count": round(
                sum(int(result["recommendation_count"]) for result in coverage_import_results) / len(coverage_import_results),
                3,
            ),
        }
    if triage_results:
        metrics["triage"] = {
            "cases": len(triage_results),
            "avg_cluster_count": round(sum(int(result["cluster_count"]) for result in triage_results) / len(triage_results), 3),
            "avg_evidence_hits": round(sum(int(result["evidence_hits"]) for result in triage_results) / len(triage_results), 3),
        }
    provider_response_results = [result for result in results if result["task_type"] == "provider_response"]
    if provider_response_results:
        metrics["provider_response"] = {
            "cases": len(provider_response_results),
            "provider_error_count": sum(1 for result in provider_response_results if result.get("status") == "provider_error"),
            "generated_candidate_count": sum(1 for result in provider_response_results if bool(result.get("generated_candidate"))),
            "validation_statuses": _count_values(provider_response_results, "validation_status"),
        }
    metrics["readiness"] = _readiness_metrics(results)
    return metrics


def _count_values(results: list[dict[str, object]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        value = str(result.get(key, "unknown") or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _readiness_metrics(results: list[dict[str, object]]) -> dict[str, object]:
    tool_backed_values = {"fixture_tool", "adapter_backed", "formal_tool", "executable_sim", "tool_backed"}
    structure_only_values = {"fixture_structure", "structure_only", "syntax_plus_structure"}
    return {
        "case_scope_counts": _count_values(results, "benchmark_scope"),
        "execution_backing_counts": _count_values(results, "execution_backing"),
        "validation_status_counts": _count_present_values(results, "validation_status"),
        "validation_mode_counts": _count_present_values(results, "validation_mode"),
        "overall_status_counts": _count_present_values(results, "overall_status"),
        "benchmark_status_counts": _count_present_values(results, "benchmark_status"),
        "tool_backed_case_count": sum(1 for result in results if str(result.get("execution_backing")) in tool_backed_values),
        "structure_or_fixture_only_case_count": sum(
            1
            for result in results
            if (
                str(result.get("execution_backing")) not in tool_backed_values
                and (
                    str(result.get("execution_backing")) in structure_only_values
                    or str(result.get("execution_backing", "")).startswith("fixture_")
                )
            )
        ),
        "interpretation": "Default benchmarks are deterministic fixture regressions unless execution_backing names an adapter/formal/executable tool.",
    }


def _count_present_values(results: list[dict[str, object]], key: str) -> dict[str, int]:
    values = [result for result in results if result.get(key) not in {None, "", "not_run"}]
    return _count_values(values, key)


def _case_scope(case: BenchmarkCase) -> str:
    return str(case.scoring.get("benchmark_scope", case.config.get("benchmark_scope", "fixture")))


def _case_execution_backing(case: BenchmarkCase, default: str) -> str:
    return str(case.scoring.get("execution_backing", case.config.get("execution_backing", default)))


def _validation_backing(validation_mode: str, *, formal_status: str | None = None, executable_status: str | None = None) -> str:
    if formal_status and formal_status not in {"", "not_run", "skipped"}:
        return "formal_tool"
    if executable_status and executable_status not in {"", "not_run", "skipped"}:
        return "executable_sim"
    if validation_mode in {"adapter", "adapter_plus_structure", "adapter_backed"}:
        return "adapter_backed"
    if validation_mode in {"syntax_plus_structure", "structure_only", "builtin_sva_syntax"}:
        return "structure_only"
    return validation_mode or "not_run"


def _benchmark_status(passed: bool, overall_status: str) -> str:
    if not passed:
        return "failed"
    if overall_status == "passed_with_warnings":
        return "passed_with_warnings"
    return "passed"


def _resolve_runtime_placeholders(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_runtime_placeholders(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_resolve_runtime_placeholders(item) for item in value]
    if value == "__python__":
        return sys.executable
    return value
