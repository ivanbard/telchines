from __future__ import annotations

import json
import sys

payload = json.loads(sys.stdin.read())
response = {
    "status": "proposed",
    "file_path": payload["output_file"],
    "candidate_content": """module uart_tx_assertions(
  input logic clk,
  input logic rst_n,
  input logic [3:0] tx_fifo_level,
  input logic ready_o
);

property p_ready_when_fifo_nonzero;
  @(posedge clk) disable iff (!rst_n) (tx_fifo_level != 4'd0) |-> ready_o;
endproperty

assert property (p_ready_when_fifo_nonzero);

endmodule

bind uart_tx uart_tx_assertions uart_tx_assertions_i(
  .clk(clk),
  .rst_n(rst_n),
  .tx_fifo_level(tx_fifo_level),
  .ready_o(ready_o)
);
""",
    "explanation": "Generated a UART TX FIFO-level readiness assertion.",
    "evidence_paths": [payload["spec"]["path"], payload["rtl"]["path"]],
    "properties": [
        {
            "name": "p_ready_when_fifo_nonzero",
            "summary": "Checks that the transmitter reports ready when FIFO data is present.",
            "rationale": "Grounded in the UART TX FIFO-level requirement.",
            "source_citation": payload["spec"]["path"],
        }
    ],
}
sys.stdout.write(json.dumps(response))
