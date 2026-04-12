from __future__ import annotations

from telchines.adapters.open_tools import SymbiYosysAdapter
from telchines.adapters.registry import AdapterRegistry


def test_adapter_registry_lists_categories() -> None:
    registry = AdapterRegistry()
    simulation_names = [adapter.name for adapter in registry.list(category="simulation")]
    assert "verilator" in simulation_names
    assert "iverilog" in simulation_names
    assert "slang" in simulation_names
    assert [adapter.name for adapter in registry.list(category="formal")] == ["symbiyosys"]


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
