from __future__ import annotations

import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from telchines.adapters.parsing import parse_common_output
from telchines.config import ProjectConfig
from telchines.models import BenchmarkCase, ToolReference, VerificationRun
from telchines.providers import build_generation_provider, build_repair_provider
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import copy_tree_to_temp, read_json, remove_tree, stable_id, utc_now
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
    benchmarks_root = config.project_root / "benchmarks"
    cases = load_benchmark_cases(benchmarks_root)
    retrieval = RetrievalService(config)
    retrieval.build_index()
    provider = build_repair_provider(config)
    results: list[dict[str, object]] = []
    for case in cases:
        if case.task_type == "repair":
            results.append(_run_repair_case(config, store, retrieval, provider, case))
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
    passed = sum(1 for result in results if result["passed"])
    report = {
        "suite": "default",
        "ran_at": utc_now(),
        "cases": results,
        "passed": passed,
        "total": len(results),
        "metrics": _aggregate_metrics(results),
    }
    store.save_report("latest_eval", report)
    return report


def _run_repair_case(config: ProjectConfig, store: RunStore, retrieval: RetrievalService, provider, case: BenchmarkCase) -> dict[str, object]:
    fixture_root = config.project_root / case.fixture_root
    temp_root = copy_tree_to_temp(fixture_root)
    try:
        command = [sys.executable, *case.config["validator_command"]]
        process = subprocess.run(command, cwd=temp_root, capture_output=True, text=True, check=False)
        run_id = stable_id("run", case.benchmark_id, "initial")
        observations = parse_common_output(run_id, process.stdout + process.stderr)
        store.save_observations(observations)
        base_run = VerificationRun(
            run_id=run_id,
            project_id=config.project.project_id,
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
        store.save_run(base_run)
        original_root = config.project.root_path
        config.project.root_path = str(temp_root)
        try:
            proposal, validation_run, _ = execute_repair(config, store, retrieval, provider, base_run, apply_patch=False)
        finally:
            config.project.root_path = original_root
        expected = str(case.scoring.get("expected", "pass"))
        if expected == "no_patch":
            passed = proposal is None and validation_run is None
        else:
            passed = validation_run is not None and validation_run.status == "passed"
        return {
            "benchmark_id": case.benchmark_id,
            "task_type": case.task_type,
            "passed": passed,
            "patch_id": proposal.patch_id if proposal else None,
        }
    finally:
        remove_tree(temp_root)


def _run_triage_case(config: ProjectConfig, store: RunStore, retrieval: RetrievalService, case: BenchmarkCase) -> dict[str, object]:
    logs_path = config.project_root / case.fixture_root / case.config["logs_path"]
    run, clusters, _ = triage_logs(config, store, retrieval, logs_path)
    expected = int(case.scoring.get("min_clusters", 1))
    passed = len(clusters) >= expected
    return {
        "benchmark_id": case.benchmark_id,
        "task_type": case.task_type,
        "passed": passed,
        "run_id": run.run_id,
        "cluster_count": len(clusters),
        "evidence_hits": sum(len(cluster.evidence_hits) for cluster in clusters),
    }


def _run_retrieval_case(config: ProjectConfig, case: BenchmarkCase) -> dict[str, object]:
    fixture_root = config.project_root / case.fixture_root
    temp_root = copy_tree_to_temp(fixture_root)
    try:
        temp_config = ProjectConfig.init_project(temp_root)
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
        temp_config = ProjectConfig.init_project(temp_root)
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
            "provider": provider.name,
            "context_id": context.context_id,
            "generated_candidate": generated_candidate,
            "candidate_id": candidate.candidate_id if candidate else None,
            "validation_run_id": validation_run.run_id if validation_run else None,
            "validation_status": validation_status,
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
        temp_config = ProjectConfig.init_project(temp_root)
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
        )
        return {
            "benchmark_id": case.benchmark_id,
            "task_type": case.task_type,
            "passed": passed,
            "provider": provider.name,
            "context_id": context.context_id,
            "run_id": run.run_id if run else None,
            "candidate_id": candidate.candidate_id if candidate else None,
            "validation_run_id": validation_run.run_id if validation_run else None,
            "validation_status": validation_status,
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
        temp_config = ProjectConfig.init_project(temp_root)
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


def _aggregate_metrics(results: list[dict[str, object]]) -> dict[str, object]:
    retrieval_results = [result for result in results if result["task_type"] == "retrieval"]
    sva_results = [result for result in results if result["task_type"] == "sva"]
    cocotb_results = [result for result in results if result["task_type"] == "cocotb"]
    coverage_results = [result for result in results if result["task_type"] == "coverage"]
    triage_results = [result for result in results if result["task_type"] == "triage"]
    metrics: dict[str, object] = {}
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
    if triage_results:
        metrics["triage"] = {
            "cases": len(triage_results),
            "avg_cluster_count": round(sum(int(result["cluster_count"]) for result in triage_results) / len(triage_results), 3),
            "avg_evidence_hits": round(sum(int(result["evidence_hits"]) for result in triage_results) / len(triage_results), 3),
        }
    return metrics


def _resolve_runtime_placeholders(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_runtime_placeholders(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_resolve_runtime_placeholders(item) for item in value]
    if value == "__python__":
        return sys.executable
    return value
