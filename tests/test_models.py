from __future__ import annotations

from ovai.models import ToolReference, VerificationProject, VerificationRun
from ovai.utils import dataclass_to_dict


def test_model_serialization() -> None:
    project = VerificationProject(project_id="proj_1", name="demo", root_path="/tmp/demo", created_at="2026-04-07T00:00:00+00:00")
    run = VerificationRun(
        run_id="run_1",
        project_id=project.project_id,
        commit_sha="workspace",
        workflow_type="compile_repair",
        tool=ToolReference(kind="simulator", name="verilator"),
        inputs={"files": ["rtl/demo.sv"]},
        status="failed",
        started_at="2026-04-07T00:00:00+00:00",
    )
    payload = dataclass_to_dict(run)
    assert payload["schema_version"] == "0.1"
    assert payload["tool"]["name"] == "verilator"
