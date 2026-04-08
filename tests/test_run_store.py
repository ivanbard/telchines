from __future__ import annotations

from pathlib import Path

from telchines.config import ProjectConfig
from telchines.models import ToolReference, VerificationRun
from telchines.run_store import RunStore


def test_run_store_round_trip(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    run = VerificationRun(
        run_id="run_1",
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="compile_repair",
        tool=ToolReference(kind="simulator", name="verilator"),
        inputs={"files": ["rtl/broken_counter.sv"]},
        status="failed",
        started_at="2026-04-07T00:00:00+00:00",
    )
    store.save_run(run)
    loaded = store.load_run("run_1")
    assert loaded.tool.name == "verilator"
    assert loaded.inputs["files"] == ["rtl/broken_counter.sv"]
