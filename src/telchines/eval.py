from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from telchines.adapters.parsing import parse_common_output
from telchines.config import ProjectConfig
from telchines.models import BenchmarkCase, ToolReference, VerificationRun
from telchines.providers import build_repair_provider
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import copy_tree_to_temp, read_json, remove_tree, stable_id, utc_now
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
        min_recall = float(case.scoring.get("min_recall", 1.0))
        min_citation_coverage = float(case.scoring.get("min_citation_coverage", 1.0))
        passed = recall >= min_recall and citation_coverage >= min_citation_coverage
        return {
            "benchmark_id": case.benchmark_id,
            "task_type": case.task_type,
            "passed": passed,
            "mode": context.mode,
            "query": context.query,
            "hit_count": len(context.hits),
            "matched_paths": matched_paths,
            "recall_at_k": round(recall, 3),
            "citation_coverage": round(citation_coverage, 3),
        }
    finally:
        remove_tree(temp_root)


def _aggregate_metrics(results: list[dict[str, object]]) -> dict[str, object]:
    retrieval_results = [result for result in results if result["task_type"] == "retrieval"]
    triage_results = [result for result in results if result["task_type"] == "triage"]
    metrics: dict[str, object] = {}
    if retrieval_results:
        metrics["retrieval"] = {
            "cases": len(retrieval_results),
            "avg_recall_at_k": round(sum(float(result["recall_at_k"]) for result in retrieval_results) / len(retrieval_results), 3),
            "avg_citation_coverage": round(
                sum(float(result["citation_coverage"]) for result in retrieval_results) / len(retrieval_results),
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
