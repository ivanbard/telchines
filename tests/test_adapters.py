from __future__ import annotations

from pathlib import Path

from telchines.adapters.open_tools import IcarusAdapter, SlangAdapter, SymbiYosysAdapter, VeribleAdapter, VerilatorAdapter
from telchines.adapters.registry import AdapterRegistry


def test_adapter_registry_lists_categories() -> None:
    registry = AdapterRegistry()
    simulation_names = [adapter.name for adapter in registry.list(category="simulation")]
    assert "verilator" in simulation_names
    assert "iverilog" in simulation_names
    assert "slang" in simulation_names
    assert [adapter.name for adapter in registry.list(category="formal")] == ["symbiyosys"]
    iverilog = next(adapter for adapter in registry.list(category="simulation") if adapter.name == "iverilog")
    descriptor = iverilog.describe(enabled=True)
    assert descriptor.validation_mode == "compile_and_run"
    assert descriptor.required_binaries == ["iverilog", "vvp"]


def test_symbiyosys_adapter_parses_structured_results(work_root) -> None:
    trace_path = work_root / "engine_0" / "trace.vcd"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text("$date today $end\n", encoding="utf-8")
    report_path = work_root / "engine_0" / "summary.txt"
    report_path.write_text("formal summary\n", encoding="utf-8")

    combined = """
SBY 12:00:00 [proof] engine_0: status: failed
SBY 12:00:00 [proof] uart_start_seen failed
SBY 12:00:00 [proof] counterexample trace: engine_0/trace.vcd
SBY 12:00:00 [proof] report stored in engine_0/summary.txt
"""
    result = SymbiYosysAdapter().parse_result(work_root, ["proof.sby"], "", "", combined)
    assert result["status"] == "failed"
    assert "uart_start_seen" in result["property_ids"]
    assert "engine_0/trace.vcd" in result["counterexample_paths"]
    assert "engine_0/summary.txt" in result["report_paths"]


def test_adapter_parser_handles_realistic_tool_output_shapes() -> None:
    samples = {
        "verilator": (
            VerilatorAdapter(),
            "%Error: rtl/uart_rx.sv:42:13: syntax error, unexpected endmodule\n"
            "%Warning-WIDTH: rtl/uart_rx.sv:55:21: Operator ADD expects 8 bits on the RHS\n",
        ),
        "iverilog": (
            IcarusAdapter(),
            "rtl/fifo.sv:17: syntax error\n"
            "rtl/fifo.sv:18: error: malformed statement\n",
        ),
        "slang": (
            SlangAdapter(),
            "error: rtl/counter.sv:9:5: use of undeclared identifier 'next_count'\n",
        ),
        "verible": (
            VeribleAdapter(),
            "rtl/top.sv:12:7: syntax error at token \"assign\"\n",
        ),
        "symbiyosys": (
            SymbiYosysAdapter(),
            "Assert failed in p_ready_when_valid at rtl/uart_tx.sv:88: counterexample generated\n",
        ),
    }

    parsed = {name: adapter.parse_output(f"run_{name}", text) for name, (adapter, text) in samples.items()}

    assert [obs.signature for obs in parsed["verilator"]] == ["SV_EXPECTED_ENDMODULE", "SV_WIDTH_WARNING"]
    assert (parsed["verilator"][0].file or "").replace("\\", "/") == "rtl/uart_rx.sv"
    assert parsed["verilator"][0].line == 42
    assert parsed["verilator"][1].severity == "warning"
    assert [obs.signature for obs in parsed["iverilog"]] == ["SV_GENERIC_SYNTAX_ERROR", "SV_MALFORMED_STATEMENT"]
    assert parsed["slang"][0].signature == "SV_UNKNOWN_IDENTIFIER"
    assert parsed["verible"][0].signature == "SV_GENERIC_SYNTAX_ERROR"
    assert parsed["symbiyosys"][0].signature == "ASSERTION_FAILURE"
    assert (parsed["symbiyosys"][0].file or "").replace("\\", "/") == "rtl/uart_tx.sv"


def test_iverilog_adapter_runs_compile_and_run(monkeypatch, work_root: Path) -> None:
    commands: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(command, cwd, capture_output, text, check):  # noqa: ANN001
        commands.append(command)
        if command[0] == "iverilog":
            output_path = Path(command[command.index("-o") + 1])
            output_path.write_text("compiled", encoding="utf-8")
            return Result(0, "", "")
        return Result(0, "simulation passed\n", "")

    monkeypatch.setattr("telchines.adapters.base.shutil.which", lambda _: "tool.exe")
    monkeypatch.setattr("telchines.adapters.open_tools.subprocess.run", fake_run)

    adapter = IcarusAdapter()
    execution = adapter.run("run_iverilog", work_root, ["rtl/demo.sv"], work_root / "artifacts")
    assert execution.exit_code == 0
    assert commands[0][:3] == ["iverilog", "-g2012", "-o"]
    assert commands[1][0] == "vvp"
    assert execution.result["validation_mode"] == "compile_and_run"
    assert execution.result["compile_exit_code"] == 0
    assert execution.result["run_exit_code"] == 0
    assert execution.artifacts["compiled_executable"].endswith("run_iverilog.out")
