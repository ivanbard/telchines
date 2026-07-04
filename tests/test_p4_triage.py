from __future__ import annotations

from pathlib import Path

from telchines.operations import format_triage_ci, format_triage_human, triage


def test_mixed_p4_triage_outputs_domain_metadata(sample_project: Path) -> None:
    logs_dir = sample_project / "logs" / "p4"
    logs_dir.mkdir(parents=True)
    (logs_dir / "uvm.log").write_text(
        "\n".join(
            [
                "UVM_FATAL tb/uvm/env.svh(88) @ 1000ns: uvm_test_top.env [PHASE_TIMEOUT] run phase objection timeout",
                "UVM_ERROR tb/uvm/sb.svh(31) @ 1200ns: uvm_test_top.env.scoreboard [SB_MISMATCH] scoreboard mismatch expected 0x55 observed 0x54",
            ]
        ),
        encoding="utf-8",
    )
    (logs_dir / "vendor.log").write_text(
        "\n".join(
            [
                "ERROR: [Synth 8-439] module uart_top not found [rtl/top.sv:17]",
                "Error (14566): The Fitter cannot place component at rtl/top.sv(21)",
                "ERROR: TIMING-003: Timing constraint violation File: constraints/top.pdc, Line: 12",
            ]
        ),
        encoding="utf-8",
    )

    payload = triage(sample_project, [logs_dir])
    signatures = {cluster["signature"] for cluster in payload["clusters"]}
    human = format_triage_human(payload)
    ci = format_triage_ci(payload)

    assert {"UVM_PHASE_OBJECTION_TIMEOUT", "UVM_SCOREBOARD_MISMATCH", "VIVADO_SYNTH_ERROR", "QUARTUS_FITTER_ERROR", "LIBERO_TIMING_ERROR"} <= signatures
    assert any(cluster["domain"] == "uvm_testbench" for cluster in payload["clusters"])
    assert any(cluster["domain"] == "fpga_vendor_build" for cluster in payload["clusters"])
    assert "domain: uvm_testbench / uvm / uvm" in human
    assert any(cluster["log_family"] == "uvm" for cluster in ci["clusters"])
    assert any(cluster["tool_name"] == "vivado" for cluster in ci["clusters"])
