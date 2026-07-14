module vendor_board_top (input logic clk_50m_i, input logic rst_n, output logic pll_locked_o);
  vendor_pll_wrapper u_vendor_pll (.clk_i(clk_50m_i), .rst_n(rst_n), .clk_locked_o(pll_locked_o));
endmodule
