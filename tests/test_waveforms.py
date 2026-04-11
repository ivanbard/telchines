from __future__ import annotations

from pathlib import Path

from telchines.operations import inspect_waveform, list_waveforms, show_waveform, triage, waveform_signals


def test_waveform_operations_parse_and_persist_summary(sample_project: Path) -> None:
    payload = show_waveform(sample_project, "logs/regressions/uart_rx_trace.vcd")
    assert payload["format"] == "vcd"
    assert payload["timescale"] == "1ns"
    assert any(signal["full_name"].endswith("start_seen") for signal in payload["signals"])

    listed = list_waveforms(sample_project)
    assert listed["waveforms"]
    assert listed["waveforms"][0]["source_path"].endswith("uart_rx_trace.vcd")


def test_waveform_signals_and_inspect_return_transitions(sample_project: Path) -> None:
    triage(sample_project, [sample_project / "logs" / "regressions"])
    signals = waveform_signals(sample_project, "logs/regressions/uart_rx_trace.vcd", signal_filter="start")
    assert signals["signal_count"] >= 1
    assert any(item["name"] == "start_seen" for item in signals["signals"])

    inspect = inspect_waveform(sample_project, "logs/regressions/uart_rx_trace.vcd", signal="start_seen", window=4)
    assert inspect["signal_name"] == "start_seen"
    assert inspect["transition_count"] >= 2
    assert inspect["transitions"][0]["timestamp"] == 0
