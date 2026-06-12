from __future__ import annotations

from pathlib import Path

from telchines.config import ProjectConfig
from telchines.models import ToolReference, VerificationRun
from telchines.run_store import RunStore
from telchines.utils import read_json


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
        tool_result={"status": "failed", "validation_mode": "compile_only", "assumptions": ["compile only fallback"]},
    )
    store.save_run(run)
    loaded = store.load_run("run_1")
    assert loaded.tool.name == "verilator"
    assert loaded.inputs["files"] == ["rtl/broken_counter.sv"]
    assert loaded.tool_result["validation_mode"] == "compile_only"
    assert loaded.tool_result["assumptions"] == ["compile only fallback"]


def test_run_store_redacts_sensitive_task_artifact_fields(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    path = store.save_task_artifact(
        "task_secret",
        "provider_request",
        {
            "headers": {"Authorization": "Bearer real-token", "X-Trace": "ok"},
            "env": {"TELCHINES_API_KEY": "real-token", "SAFE_VALUE": "ok"},
            "nested": {"password": "pw", "content": "keep"},
        },
    )
    payload = read_json(path)
    assert payload["headers"]["Authorization"] == "<redacted>"
    assert payload["headers"]["X-Trace"] == "ok"
    assert payload["env"]["TELCHINES_API_KEY"] == "<redacted>"
    assert payload["env"]["SAFE_VALUE"] == "ok"
    assert payload["nested"]["password"] == "<redacted>"
    assert payload["nested"]["content"] == "keep"
