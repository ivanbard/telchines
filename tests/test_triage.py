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
    assert clusters[0].likely_cause
    assert clusters[0].suggested_action
    assert clusters[0].evidence_context_id
    assert clusters[0].evidence_hits
    assert any(hit.path.endswith("uart_rx.sv") or hit.path.endswith("uart.md") for hit in clusters[0].evidence_hits)


def test_triage_finds_similar_previous_runs(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    retrieval = RetrievalService(config)
    retrieval.build_index()
    first_run, _, _ = triage_logs(config, store, retrieval, sample_project / "logs" / "regressions")
    _, clusters, _ = triage_logs(config, store, retrieval, sample_project / "logs" / "regressions")
    assert any(match.run_id == first_run.run_id for match in clusters[0].similar_runs)
