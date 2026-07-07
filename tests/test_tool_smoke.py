from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_tool_smoke_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "tool_smoke.py"
    spec = importlib.util.spec_from_file_location("telchines_tool_smoke_test_module", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_smoke_exercises_filelist_include_define_and_top(monkeypatch) -> None:
    module = _load_tool_smoke_module()
    captures: list[dict[str, object]] = []

    class RecordingAdapter:
        binary_names = ("fake-tool",)
        required_binaries = ("fake-tool",)

        def missing_binaries(self):  # noqa: ANN001
            return []

        def run(self, run_id, project_root, files, artifacts_dir, spec=None):  # noqa: ANN001
            captures.append(
                {
                    "run_id": run_id,
                    "project_root": project_root,
                    "files": files,
                    "artifacts_dir": artifacts_dir,
                    "spec": spec,
                    "expanded": spec.expanded(project_root),
                }
            )
            return SimpleNamespace(exit_code=0, summary="ok")

    monkeypatch.setattr(
        module,
        "ADAPTERS",
        {
            "iverilog": RecordingAdapter,
            "verilator": RecordingAdapter,
            "slang": RecordingAdapter,
            "symbiyosys": RecordingAdapter,
        },
    )
    monkeypatch.setattr(module, "_adapter_available", lambda adapter: True)
    monkeypatch.setattr(sys, "argv", ["tool_smoke.py", "--adapters", "iverilog", "verilator", "slang", "symbiyosys"])

    assert module.main() == 0

    iverilog = next(item for item in captures if item["run_id"] == "smoke_iverilog")
    iverilog_expanded = iverilog["expanded"]
    assert iverilog["files"] == ["rtl/smoke_counter.sv", "rtl/smoke_counter_tb.sv"]
    assert iverilog["spec"].filelists == ["smoke_files.f"]
    assert iverilog_expanded.files == ["rtl/smoke_counter.sv", "rtl/smoke_counter_tb.sv"]
    assert iverilog_expanded.include_dirs == ["rtl/include"]
    assert iverilog_expanded.defines == ["TELCHINES_TOOL_SMOKE=1"]
    assert iverilog_expanded.top_module == "smoke_counter_tb"

    verilator = next(item for item in captures if item["run_id"] == "smoke_verilator")
    verilator_expanded = verilator["expanded"]
    assert verilator["files"] == ["rtl/smoke_counter.sv"]
    assert verilator_expanded.include_dirs == ["rtl/include"]
    assert verilator_expanded.defines == ["TELCHINES_TOOL_SMOKE=1"]
    assert verilator_expanded.top_module == "smoke_counter"

    slang = next(item for item in captures if item["run_id"] == "smoke_slang")
    slang_expanded = slang["expanded"]
    assert slang["files"] == ["rtl/smoke_counter.sv"]
    assert slang_expanded.include_dirs == ["rtl/include"]
    assert slang_expanded.defines == ["TELCHINES_TOOL_SMOKE=1"]
    assert slang_expanded.top_module == "smoke_counter"

    symbiyosys = next(item for item in captures if item["run_id"] == "smoke_symbiyosys")
    assert symbiyosys["files"] == ["smoke_counter.sby"]
    assert symbiyosys["expanded"].files == ["smoke_counter.sby"]


def test_tool_smoke_allow_missing_skips_without_failure(monkeypatch, capsys) -> None:
    module = _load_tool_smoke_module()
    calls: list[str] = []

    class MissingAdapter:
        binary_names = ("missing-tool",)
        required_binaries = ("missing-tool",)

        def missing_binaries(self):  # noqa: ANN001
            return ["missing-tool"]

        def setup_diagnostics(self, missing=None):  # noqa: ANN001
            return [f"install hint for {', '.join(missing or [])}"]

        def run(self, *args, **kwargs):  # noqa: ANN001
            calls.append("run")
            return SimpleNamespace(exit_code=0, summary="unexpected")

    monkeypatch.setattr(module, "ADAPTERS", {"iverilog": MissingAdapter})
    monkeypatch.setattr(module, "_adapter_available", lambda adapter: False)
    monkeypatch.setattr(module, "_adapter_missing_binaries", lambda adapter: ["missing-tool"])
    monkeypatch.setattr(sys, "argv", ["tool_smoke.py", "--adapters", "iverilog", "--allow-missing"])

    assert module.main() == 0
    assert calls == []
    output = capsys.readouterr().out
    assert "SKIP iverilog: missing required binaries: missing-tool" in output
    assert "install hint for missing-tool" in output


def test_tool_smoke_cocotb_lane_is_explicit(monkeypatch) -> None:
    module = _load_tool_smoke_module()
    calls: list[tuple[str, bool]] = []

    class RecordingAdapter:
        binary_names = ("fake-tool",)
        required_binaries = ("fake-tool",)

        def missing_binaries(self):  # noqa: ANN001
            return []

        def run(self, run_id, project_root, files, artifacts_dir, spec=None):  # noqa: ANN001
            return SimpleNamespace(exit_code=0, summary="ok")

    def fake_cocotb_smoke(root, failures, *, allow_missing):  # noqa: ANN001
        calls.append((str(root), allow_missing))

    monkeypatch.setattr(module, "ADAPTERS", {"iverilog": RecordingAdapter})
    monkeypatch.setattr(module, "_adapter_available", lambda adapter: True)
    monkeypatch.setattr(module, "_run_cocotb_smoke", fake_cocotb_smoke)
    monkeypatch.setattr(sys, "argv", ["tool_smoke.py", "--adapters", "iverilog", "--cocotb", "--allow-missing"])

    assert module.main() == 0
    assert calls and calls[0][1] is True
