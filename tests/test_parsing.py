from __future__ import annotations

from telchines.adapters.parsing import normalize_signature, parse_common_output


def test_common_output_parser_detects_semicolon_error() -> None:
    observations = parse_common_output("run_1", "ERROR: rtl/demo.sv:12: expected semicolon before end")
    assert len(observations) == 1
    assert observations[0].signature == "SV_PARSE_EXPECTED_SEMICOLON"
    assert normalize_signature("unknown identifier tx_fifo_level") == "SV_UNKNOWN_IDENTIFIER"
