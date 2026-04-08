from __future__ import annotations

from pathlib import Path

from ovai.config import ProjectConfig
from ovai.retrieval import RetrievalService
from ovai.run_store import RunStore
from ovai.workflows.triage import triage_logs


def test_triage_clusters_failures(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    retrieval = RetrievalService(config)
    retrieval.build_index()
    run, clusters, _ = triage_logs(config, store, retrieval, sample_project / "logs" / "regressions")
    assert run.workflow_type == "regression_triage"
    assert len(clusters) == 2
    assert clusters[0].count == 3
