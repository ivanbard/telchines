from __future__ import annotations

from pathlib import Path

from ovai.config import ProjectConfig
from ovai.retrieval import RetrievalService


def test_retrieval_builds_and_searches(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    retrieval = RetrievalService(config)
    chunk_count = retrieval.build_index()
    context = retrieval.search("counter reset")
    assert chunk_count > 0
    assert context.hits
    assert any(hit.path.endswith("spec.md") for hit in context.hits)
