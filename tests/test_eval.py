from __future__ import annotations

import shutil
from pathlib import Path

from telchines.config import ProjectConfig
from telchines.eval import run_default_suite
from telchines.run_store import RunStore


def test_eval_default_suite(work_root: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    project_root = work_root / "eval_project"
    project_root.mkdir()
    shutil.copytree(repo_root / "benchmarks", project_root / "benchmarks")
    ProjectConfig.init_project(project_root)
    config = ProjectConfig.load(project_root)
    store = RunStore(config)
    report = run_default_suite(config, store)
    assert report["passed"] == report["total"]
    assert report["total"] == 8
    assert report["metrics"]["retrieval"]["cases"] == 2
    assert report["metrics"]["retrieval"]["avg_recall_at_k"] == 1.0
    assert report["metrics"]["triage"]["cases"] == 2
