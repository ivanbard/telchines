from __future__ import annotations

import json
import sys

payload = json.loads(sys.stdin.read())
response = {
    "status": "proposed",
    "file_path": payload["output_file"],
    "candidate_content": """module uart_rx_assertions(
  input logic clk,
  input logic rst_n,
  input logic serial_i,
  input logic start_seen
);

property p_start_seen_after_start_bit;
  @(posedge clk) disable iff (!rst_n) (!serial_i) |=> start_seen;

assert property (p_start_seen_after_start_bit);
""",
    "explanation": "Generated an intentionally invalid UART RX assertion candidate for validation benchmarking.",
    "evidence_paths": [payload["spec"]["path"], payload["rtl"]["path"]],
    "properties": [
        {
            "name": "p_start_seen_after_start_bit",
            "summary": "Checks that a detected start bit leads to start_seen on the next cycle.",
            "rationale": "Grounded in the UART RX start-bit requirement.",
            "source_citation": payload["spec"]["path"],
        }
    ],
}
sys.stdout.write(json.dumps(response))
