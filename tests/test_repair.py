from __future__ import annotations

import sys
from pathlib import Path

from telchines.adapters.base import AdapterExecution, ToolAdapter
from telchines.config import ProjectConfig
from telchines.models import PatchProposal, ToolReference, VerificationRun
from telchines.run_store import RunStore
from telchines.workflows.repair import validate_patch


class FakeValidationAdapter(ToolAdapter):
    name = "iverilog"
    kind = "simulator"
    category = "simulation"
    validation_mode = "compile_and_run"
    supported_workflows = ("repair_validation",)

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["iverilog", *files]

    def run(self, run_id: str, project_root: Path, files: list[str], artifacts_dir: Path, extra_args: list[str] | None = None) -> AdapterExecution:
        return AdapterExecution(
            command=["iverilog", *files],
            cwd=str(project_root),
            exit_code=0,
            stdout="simulation passed",
            stderr="",
            log_path=str(artifacts_dir / f"{run_id}.log"),
            started_at="2026-04-13T00:00:00+00:00",
            finished_at="2026-04-13T00:00:01+00:00",
            observations=[],
            summary="iverilog compile+run passed",
            artifacts={"log_path": str(artifacts_dir / f"{run_id}.log"), "compiled_executable": str(artifacts_dir / f"{run_id}.out")},
            result={"status": "passed", "validation_mode": "compile_and_run", "compile_exit_code": 0, "run_exit_code": 0},
        )


class FakeRegistry:
    def get(self, name: str) -> ToolAdapter:
        if name != "iverilog":
            raise KeyError(name)
        return FakeValidationAdapter()


def test_validate_patch_prefers_adapter_aware_validation(sample_project: Path, monkeypatch) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    base_run = VerificationRun(
        run_id="run_base",
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="compile_repair",
        tool=ToolReference(kind="simulator", name="iverilog"),
        inputs={"files": ["rtl/broken_counter.sv"], "extra_args": []},
        status="failed",
        started_at="2026-04-13T00:00:00+00:00",
        replay_command=["iverilog", "rtl/broken_counter.sv"],
    )
    proposal = PatchProposal(
        patch_id="patch_1",
        task_id="task_1",
        based_on_observations=[],
        file_path="rtl/broken_counter.sv",
        diff="",
        candidate_content=(sample_project / "rtl" / "broken_counter.sv").read_text(encoding="utf-8").replace("count <= 4'd0", "count <= 4'd0;"),
        explanation="fix",
        status="proposed",
    )
    monkeypatch.setattr("telchines.workflows.repair.AdapterRegistry", FakeRegistry)
    validation_run = validate_patch(config, store, base_run, proposal, apply_patch=False)
    assert validation_run.status == "passed"
    assert validation_run.tool_result["validation_mode"] == "compile_and_run"
    assert validation_run.artifacts["compiled_executable"].endswith(".out")


def test_validate_patch_reuses_stored_run_spec(sample_project: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class RunSpecAdapter(FakeValidationAdapter):
        def run(self, run_id, project_root, files, artifacts_dir, extra_args=None, spec=None):  # noqa: ANN001
            captured["project_root"] = project_root
            captured["files"] = files
            captured["extra_args"] = extra_args
            captured["spec"] = spec
            captured["expanded"] = spec.expanded(project_root)
            return super().run(run_id, project_root, files, artifacts_dir, extra_args=extra_args)

    class RunSpecRegistry:
        def get(self, name: str) -> ToolAdapter:
            assert name == "iverilog"
            return RunSpecAdapter()

    (sample_project / "design.f").write_text(
        "\n".join(["+incdir+rtl/include", "+define+SIM=1", "rtl/broken_counter.sv", ""]),
        encoding="utf-8",
    )
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    base_run = VerificationRun(
        run_id="run_base",
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="compile_repair",
        tool=ToolReference(kind="simulator", name="iverilog"),
        inputs={
            "files": [],
            "extra_args": ["--legacy"],
            "run_spec": {
                "filelists": ["design.f"],
                "include_dirs": ["rtl/local"],
                "defines": ["LOCAL=1"],
                "top_module": "broken_counter",
                "extra_args": ["--from-spec"],
            },
        },
        status="failed",
        started_at="2026-04-13T00:00:00+00:00",
        replay_command=["iverilog", "rtl/broken_counter.sv"],
    )
    proposal = PatchProposal(
        patch_id="patch_1",
        task_id="task_1",
        based_on_observations=[],
        file_path="rtl/broken_counter.sv",
        diff="",
        candidate_content=(sample_project / "rtl" / "broken_counter.sv").read_text(encoding="utf-8").replace("count <= 4'd0", "count <= 4'd0;"),
        explanation="fix",
        status="proposed",
    )
    monkeypatch.setattr("telchines.workflows.repair.AdapterRegistry", RunSpecRegistry)

    validation_run = validate_patch(config, store, base_run, proposal, apply_patch=False)

    expanded = captured["expanded"]
    assert validation_run.status == "passed"
    assert captured["files"] == []
    assert captured["extra_args"] == ["--legacy"]
    assert expanded.files == ["rtl/broken_counter.sv"]
    assert expanded.include_dirs == ["rtl/local", "rtl/include"]
    assert expanded.defines == ["LOCAL=1", "SIM=1"]
    assert expanded.top_module == "broken_counter"
    assert expanded.extra_args == ["--from-spec"]


def test_validate_patch_falls_back_to_adapter_replay(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    base_run = VerificationRun(
        run_id="run_base",
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="compile_repair",
        tool=ToolReference(kind="linter", name="fixture"),
        inputs={"files": ["rtl/broken_counter.sv"], "extra_args": []},
        status="failed",
        started_at="2026-04-13T00:00:00+00:00",
        replay_command=[sys.executable, "tools/fixture_lint.py", "rtl/broken_counter.sv"],
    )
    proposal = PatchProposal(
        patch_id="patch_1",
        task_id="task_1",
        based_on_observations=[],
        file_path="rtl/broken_counter.sv",
        diff="",
        candidate_content=(sample_project / "rtl" / "broken_counter.sv").read_text(encoding="utf-8").replace("count <= 4'd0", "count <= 4'd0;"),
        explanation="fix",
        status="proposed",
    )
    validation_run = validate_patch(config, store, base_run, proposal, apply_patch=False)
    assert validation_run.status == "passed"
    assert validation_run.tool_result["validation_mode"] == "adapter_replay"
