from __future__ import annotations

from pathlib import Path

from telchines.config import ProjectConfig
from telchines.retrieval import RetrievalService


def test_retrieval_builds_and_searches(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    retrieval = RetrievalService(config)
    chunk_count = retrieval.build_index()
    context = retrieval.search("counter reset")
    assert chunk_count > 0
    assert context.hits
    assert context.mode == "general"
    assert all(hit.citation for hit in context.hits)
    assert any(hit.path.endswith("spec.md") for hit in context.hits)


def test_retrieval_supports_task_aware_ranking_and_incremental_refresh(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    retrieval = RetrievalService(config)
    retrieval.build_index()

    triage_context = retrieval.search(
        "timeout waiting for start bit",
        mode="triage",
        focus_paths=["rtl/uart_rx.sv"],
    )
    assert triage_context.mode == "triage"
    assert triage_context.metadata["focus_paths"] == ["rtl/uart_rx.sv"]
    assert triage_context.hits
    assert any(hit.path.endswith("uart_rx.sv") for hit in triage_context.hits)

    uart_doc = sample_project / "docs" / "uart.md"
    uart_doc.write_text(
        uart_doc.read_text(encoding="utf-8") + "\nThe baud_divisor register gates uart scheduling.\n",
        encoding="utf-8",
    )
    retrieval.build_index()
    refreshed = retrieval.search("baud_divisor register", mode="triage")
    assert any("baud_divisor" in hit.snippet for hit in refreshed.hits)


def test_retrieval_handles_larger_fixture_corpus(retrieval_corpus_project: Path) -> None:
    config = ProjectConfig.load(retrieval_corpus_project)
    retrieval = RetrievalService(config)
    chunk_count = retrieval.build_index()
    context = retrieval.search(
        "timeout waiting for start bit tx_fifo_level",
        mode="triage",
        focus_paths=["rtl/uart_rx.sv", "rtl/uart_tx.sv"],
        limit=6,
    )
    assert chunk_count >= 6
    assert len(context.hits) >= 3
    assert any(hit.path.endswith("uart_rx.sv") for hit in context.hits)
    assert any(hit.path.endswith("uart_spec.md") for hit in context.hits)
    assert any(hit.kind == "log" for hit in context.hits)
