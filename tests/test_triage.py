from __future__ import annotations

from pathlib import Path

from telchines.config import ProjectConfig
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.workflows.triage import triage_logs


def test_triage_clusters_failures(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    retrieval = RetrievalService(config)
    retrieval.build_index()
    run, clusters, _ = triage_logs(config, store, retrieval, sample_project / "logs" / "regressions")
    assert run.workflow_type == "regression_triage"
    assert len(clusters) == 2
    assert clusters[0].count == 3
