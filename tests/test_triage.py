from __future__ import annotations

from pathlib import Path

from telchines.config import ProjectConfig
from telchines.models import ToolReference, VerificationRun
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
    assert run.inputs["waveform_count"] == 1
    rx_cluster = next(cluster for cluster in clusters if cluster.signature == "SIM_TIMEOUT")
    assert rx_cluster.waveform_evidence
    rx_evidence = rx_cluster.waveform_evidence[0]
    assert rx_evidence.source_path.endswith("uart_rx_trace.vcd")
    assert rx_evidence.relevance == "matched"
    assert set(rx_evidence.matched_signals) & {"start_seen", "serial_i"}
    assert not set(rx_evidence.matched_signals) <= {"clk", "rst_n"}

    tx_cluster = next(cluster for cluster in clusters if cluster.signature == "SV_UNKNOWN_IDENTIFIER")
    tx_evidence = tx_cluster.waveform_evidence[0]
    assert tx_evidence.relevance == "unrelated"
    assert tx_evidence.matched_signals == []
    assert "no non-generic signal overlap" in tx_evidence.reason


def test_triage_finds_similar_previous_runs(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    retrieval = RetrievalService(config)
    retrieval.build_index()
    first_run, _, _ = triage_logs(config, store, retrieval, sample_project / "logs" / "regressions")
    _, clusters, _ = triage_logs(config, store, retrieval, sample_project / "logs" / "regressions")
    assert any(match.run_id == first_run.run_id for match in clusters[0].similar_runs)


def test_triage_supports_mixed_log_inputs(retrieval_corpus_project: Path) -> None:
    config = ProjectConfig.load(retrieval_corpus_project)
    store = RunStore(config)
    retrieval = RetrievalService(config)
    retrieval.build_index()
    logs = [
        retrieval_corpus_project / "logs" / "regressions",
        retrieval_corpus_project / "logs" / "regressions" / "nested" / "run_b.out",
    ]
    run, clusters, _ = triage_logs(config, store, retrieval, logs)
    assert run.inputs["log_file_count"] == 2
    assert len(clusters) == 2
    assert any(cluster.signature == "SIM_TIMEOUT" for cluster in clusters)
    assert any(cluster.signature == "SV_UNKNOWN_IDENTIFIER" for cluster in clusters)


def test_triage_surfaces_related_formal_evidence(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    retrieval = RetrievalService(config)
    retrieval.build_index()
    formal_run = VerificationRun(
        run_id="formal_1",
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="formal_validation",
        tool=ToolReference(kind="formal", name="symbiyosys"),
        inputs={"files": ["rtl/uart_rx.sv"]},
        status="failed",
        started_at="2026-04-13T00:00:00+00:00",
        summary="Formal run found a UART receiver start-bit failure",
        tool_result={
            "status": "failed",
            "property_ids": ["uart_start_seen_after_start_bit"],
            "counterexample_paths": ["formal/uart_rx_trace.vcd"],
            "report_paths": ["formal/summary.txt"],
        },
    )
    store.save_run(formal_run)
    _, clusters, _ = triage_logs(config, store, retrieval, sample_project / "logs" / "regressions")
    assert clusters[0].formal_evidence
    assert clusters[0].formal_evidence[0].run_id == "formal_1"
    assert clusters[0].formal_evidence[0].property_ids == ["uart_start_seen_after_start_bit"]
