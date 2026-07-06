`include "uart_defs.svh"

module uart_core #(
  parameter int BAUD_DIVISOR = `UART_DEFAULT_BAUD_DIVISOR
) (
  input  logic clk,
  input  logic rst_n,
  input  logic serial_i,
  output logic [7:0] data_o,
  output logic valid_o
);
  import uart_pkg::*;

  uart_state_e state_q;
  logic [$clog2(BAUD_DIVISOR)-1:0] baud_count_q;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state_q <= UART_IDLE;
      baud_count_q <= '0;
      valid_o <= 1'b0;
    end else begin
      valid_o <= 1'b0;
      if (state_q == UART_IDLE && !serial_i) begin
        state_q <= UART_START;
      end
      if (baud_count_q == BAUD_DIVISOR - 1) begin
        baud_count_q <= '0;
      end else begin
        baud_count_q <= baud_count_q + 1'b1;
      end
    end
  end
endmodule
