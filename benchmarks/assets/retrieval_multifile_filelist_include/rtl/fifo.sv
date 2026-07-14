module fifo #(
  parameter int DEPTH = 16
) (
  input logic clk,
  input logic rst_n,
  input logic push_i,
  output logic full_o
);
  logic [$clog2(DEPTH + 1)-1:0] level_q;
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) level_q <= '0;
    else if (push_i && !full_o) level_q <= level_q + 1'b1;
  end
  assign full_o = level_q == DEPTH;
endmodule
