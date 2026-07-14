`include "vendor_build_defs.svh"

module vendor_pll_wrapper (
  input logic clk_i,
  input logic rst_n,
  output logic clk_locked_o
);
`ifdef VENDOR_PLL_MODEL
  logic [3:0] lock_count_q;
  always_ff @(posedge clk_i or negedge rst_n) begin
    if (!rst_n) begin
      lock_count_q <= '0;
      clk_locked_o <= 1'b0;
    end else if (lock_count_q == `VENDOR_PLL_LOCK_CYCLES - 1) begin
      clk_locked_o <= 1'b1;
    end else begin
      lock_count_q <= lock_count_q + 1'b1;
    end
  end
`else
  assign clk_locked_o = rst_n;
`endif
endmodule
