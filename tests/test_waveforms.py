from __future__ import annotations

from pathlib import Path

import pytest

from telchines.models import WaveformSample, WaveformSummary
from telchines.operations import inspect_waveform, list_waveforms, show_waveform, triage, waveform_signals
from telchines.waveforms import select_signal


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
    assert inspect["match_type"] == "leaf_name"
    assert inspect["transition_count"] >= 2
    assert inspect["transitions"][0]["timestamp"] == 0

    full_name = inspect_waveform(sample_project, "logs/regressions/uart_rx_trace.vcd", signal="uart_rx_tb.start_seen", window=4)
    assert full_name["signal_name"] == "start_seen"
    assert full_name["match_type"] == "full_name"


def test_waveform_inspect_rejects_missing_signal_without_fuzzy_fallback(sample_project: Path) -> None:
    triage(sample_project, [sample_project / "logs" / "regressions"])
    with pytest.raises(ValueError, match="signal was not found") as excinfo:
        inspect_waveform(sample_project, "logs/regressions/uart_rx_trace.vcd", signal="rx", window=4)
    message = str(excinfo.value)
    assert "uart_rx_tb.serial_i" in message
    assert "uart_rx_tb.start_seen" in message


def test_select_signal_rejects_ambiguous_leaf_name() -> None:
    summary = WaveformSummary(
        waveform_id="wave_test",
        project_id="project_test",
        source_path="trace.vcd",
        source_hash="hash",
        format="vcd",
        timescale="1ns",
        sampled_signals=[
            WaveformSample(signal_name="valid", full_name="top.a.valid"),
            WaveformSample(signal_name="valid", full_name="top.b.valid"),
        ],
    )
    with pytest.raises(ValueError, match="ambiguous"):
        select_signal(summary, "valid")

    assert select_signal(summary, "top.a.valid").full_name == "top.a.valid"
