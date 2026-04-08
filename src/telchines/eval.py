from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from telchines.adapters.parsing import parse_common_output
from telchines.config import ProjectConfig
from telchines.models import BenchmarkCase, ToolReference, VerificationRun
from telchines.providers import HeuristicRepairProvider
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
    provider = HeuristicRepairProvider()
    results: list[dict[str, object]] = []
    for case in cases:
        if case.task_type == "repair":
            results.append(_run_repair_case(config, store, retrieval, provider, case))
        elif case.task_type == "triage":
            results.append(_run_triage_case(config, store, retrieval, case))
    passed = sum(1 for result in results if result["passed"])
    report = {
        "suite": "default",
        "ran_at": utc_now(),
        "cases": results,
        "passed": passed,
        "total": len(results),
    }
    store.save_report("latest_eval", report)
    return report


def _run_repair_case(config: ProjectConfig, store: RunStore, retrieval: RetrievalService, provider: HeuristicRepairProvider, case: BenchmarkCase) -> dict[str, object]:
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
    return {"benchmark_id": case.benchmark_id, "task_type": case.task_type, "passed": passed, "run_id": run.run_id}
