from __future__ import annotations

from pathlib import Path

from telchines.config import ProjectConfig
from telchines.retrieval import RetrievalService
from telchines.utils import read_json, write_json


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


def test_retrieval_merges_external_corpus_with_provenance(sample_project: Path) -> None:
    knowledge_root = sample_project / "knowledge" / "verification"
    knowledge_root.mkdir(parents=True, exist_ok=True)
    (knowledge_root / "uart_timeout.md").write_text(
        "# UART timeout debugging\n\nObjection drain time can hide the first timeout waiting for start bit in UART regressions.\n",
        encoding="utf-8",
    )
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["retrieval"]["external_roots"] = ["knowledge/verification"]
    write_json(config_path, payload)

    config = ProjectConfig.load(sample_project)
    retrieval = RetrievalService(config)
    chunk_count = retrieval.build_index()
    external_only = retrieval.search("objection drain time", mode="generation", limit=4)
    mixed = retrieval.search("timeout waiting for start bit", mode="triage", focus_paths=["rtl/uart_rx.sv"], limit=12)

    assert chunk_count > 0
    assert external_only.hits
    assert external_only.hits[0].source_domain == "external"
    assert external_only.hits[0].source_label == "knowledge/verification"
    assert external_only.hits[0].source_uri == "knowledge/verification"
    assert external_only.hits[0].ingested_at
    assert any(hit.source_domain == "external" for hit in mixed.hits)
    assert mixed.hits[0].source_domain == "project"


def test_retrieval_status_clean_and_include_exclude_patterns(sample_project: Path) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["retrieval"]["include_patterns"] = ["rtl/**", "docs/**"]
    payload["retrieval"]["exclude_patterns"] = ["docs/spec.md"]
    write_json(config_path, payload)

    config = ProjectConfig.load(sample_project)
    retrieval = RetrievalService(config)
    missing = retrieval.status()
    assert missing["status"] == "stale"
    assert missing["project"]["exists"] is False

    chunk_count = retrieval.build_index()
    assert chunk_count > 0
    indexed = read_json(sample_project / ".tel" / "index" / "index.json")
    indexed_paths = {chunk["path"] for chunk in indexed["chunks"]}
    assert "docs/spec.md" not in indexed_paths
    assert all(path.startswith(("rtl/", "docs/")) for path in indexed_paths)

    fresh = retrieval.status()
    assert fresh["status"] == "fresh"
    uart_doc = sample_project / "docs" / "uart.md"
    uart_doc.write_text(uart_doc.read_text(encoding="utf-8") + "\nNew stale marker.\n", encoding="utf-8")
    stale = retrieval.status()
    assert stale["status"] == "stale"
    assert stale["project"]["stale_source_count"] == 1

    cleaned = retrieval.clean()
    assert cleaned["removed_count"] == 2
    assert not (sample_project / ".tel" / "index").exists()
    assert not (sample_project / ".tel" / "external-index").exists()


def test_retrieval_expands_configured_domain_aliases(sample_project: Path) -> None:
    baseline_config = ProjectConfig.load(sample_project)
    baseline_retrieval = RetrievalService(baseline_config)
    baseline_retrieval.build_index()
    baseline = baseline_retrieval.search("framing pulse", mode="generation")
    assert baseline.hits == []

    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["retrieval"]["aliases"] = {"framing pulse": ["start bit", "serial_i"]}
    write_json(config_path, payload)

    config = ProjectConfig.load(sample_project)
    retrieval = RetrievalService(config)
    assert retrieval.status()["alias_count"] == 1
    aliased = retrieval.search("framing pulse", mode="generation")

    assert aliased.hits
    assert any(hit.path.endswith(("uart.md", "uart_rx.sv")) for hit in aliased.hits)
    assert aliased.metadata["query_aliases"] == {"framing pulse": ["start bit", "serial_i"]}
    assert "serial_i" in aliased.metadata["expanded_query_tokens"]
