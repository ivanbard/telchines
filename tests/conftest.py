from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from telchines.config import ProjectConfig


@pytest.fixture()
def work_root() -> Path:
    root = Path(__file__).resolve().parents[1] / ".test-work" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def sample_project(work_root: Path) -> Path:
    source = Path(__file__).parent / "fixtures" / "sample_project"
    destination = work_root / "sample_project"
    shutil.copytree(source, destination)
    ProjectConfig.init_project(destination)
    return destination


@pytest.fixture()
def retrieval_corpus_project(work_root: Path) -> Path:
    source = Path(__file__).parent / "fixtures" / "retrieval_corpus"
    destination = work_root / "retrieval_corpus"
    shutil.copytree(source, destination)
    ProjectConfig.init_project(destination)
    return destination
