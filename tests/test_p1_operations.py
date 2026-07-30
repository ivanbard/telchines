from __future__ import annotations

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from telchines import operations
from telchines import eval as eval_module
from telchines.adapters.base import AdapterExecution, ToolAdapter
from telchines.config import ProjectConfig
from telchines.models import RetrievalContext
from telchines.run_store import RunStore


class RecordingAdapter(ToolAdapter):
    name = "fixture"
    kind = "linter"
    category = "lint"
    calls: list[dict[str, object]] = []

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["fixture", *(extra_args or []), *files]

    def run(self, run_id, project_root, files, artifacts_dir, extra_args=None, spec=None):  # noqa: ANN001
        self.calls.append({"files": files, "extra_args": extra_args or [], "spec": spec})
        log_path = artifacts_dir / f"{run_id}.log"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text("fixture failed\n", encoding="utf-8")
        return AdapterExecution(
            command=self.build_command(project_root, files, extra_args),
            cwd=str(project_root),
            exit_code=1,
            stdout="",
            stderr="fixture failed\n",
            log_path=str(log_path),
            started_at="2026-07-04T00:00:00+00:00",
            finished_at="2026-07-04T00:00:01+00:00",
            observations=[],
            summary="fixture failed",
            artifacts={"log_path": str(log_path)},
            result={"status": "failed", "validation_mode": "fixture"},
        )


class RecordingRegistry:
    def __init__(self) -> None:
        self.adapter = RecordingAdapter()

    def get(self, name: str) -> RecordingAdapter:
        assert name == "fixture"
        return self.adapter


def test_repair_operation_persists_expanded_run_spec_for_filelist_only(sample_project: Path, monkeypatch) -> None:
    filelist = sample_project / "design.f"
    filelist.write_text(
        "\n".join(["+incdir+rtl/include", "+define+SIM=1", "rtl/broken_counter.sv", ""]),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_execute_repair(config, store, retrieval, provider, base_run, apply_patch=False):  # noqa: ANN001
        captured["base_run"] = base_run
        context = RetrievalContext(
            context_id="ctx_test",
            project_id=config.project.project_id,
            query="",
            hits=[],
            created_at="2026-07-04T00:00:00+00:00",
            mode="repair",
        )
        return None, None, context

    monkeypatch.setattr(operations, "AdapterRegistry", RecordingRegistry)
    monkeypatch.setattr(operations, "execute_repair", fake_execute_repair)

    payload = operations.repair(
        sample_project,
        tool="fixture",
        files=[],
        filelists=["design.f"],
        include_dirs=["rtl/local"],
        defines=["LOCAL=1"],
        top_module="broken_counter",
        adapter_args=["--lint"],
    )

    base_run = captured["base_run"]
    stored_run = RunStore(ProjectConfig.load(sample_project)).load_run(payload["run_id"])
    for run in (base_run, stored_run):
        run_spec = run.inputs["run_spec"]
        assert run.inputs["files"] == ["rtl/broken_counter.sv"]
        assert run_spec["filelists"] == ["design.f"]
        assert run_spec["include_dirs"] == ["rtl/local", "rtl/include"]
        assert run_spec["defines"] == ["LOCAL=1", "SIM=1"]
        assert run_spec["top_module"] == "broken_counter"
        assert run_spec["extra_args"] == ["--lint"]


@given(
    values=st.lists(
        st.dictionaries(
            st.text(min_size=0, max_size=8),
            st.one_of(st.none(), st.text(max_size=8), st.integers(min_value=-3, max_value=3)),
            max_size=4,
        ),
        max_size=8,
    )
)
def test_count_values_handles_missing_and_empty_values(values: list[dict[str, object]]) -> None:
    counts = eval_module._count_values(values, "validation_mode")
    expected: dict[str, int] = {}
    for value in values:
        key = str(value.get("validation_mode", "unknown") or "unknown")
        expected[key] = expected.get(key, 0) + 1
    assert counts == expected


def test_adapter_check_reports_command_preview_and_setup_diagnostics(sample_project: Path, monkeypatch) -> None:
    monkeypatch.setattr("telchines.adapters.base.shutil.which", lambda _: None)
    monkeypatch.setattr("telchines.operations.shutil.which", lambda _: None)

    payload = operations.check_adapters(sample_project, adapter_name="iverilog")
    adapter = payload["adapters"][0]

    assert adapter["status"] == "unavailable"
    assert adapter["command_preview"][:2] == ["iverilog", "-g2012"]
    assert adapter["setup_diagnostics"]
    assert "TOKEN" not in str(adapter)
    assert "secret" not in str(adapter).lower()
