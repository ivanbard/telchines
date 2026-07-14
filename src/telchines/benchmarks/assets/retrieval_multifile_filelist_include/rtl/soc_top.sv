module soc_top (input logic clk, input logic rst_n, input logic uart_rx_i, output logic rx_valid_o, output logic pll_locked_o);
  logic [7:0] rx_data;
  uart_core u_uart_core (.clk(clk), .rst_n(rst_n), .serial_i(uart_rx_i), .data_o(rx_data), .valid_o(rx_valid_o));
  vendor_pll_wrapper u_vendor_pll (.clk_i(clk), .rst_n(rst_n), .clk_locked_o(pll_locked_o));
endmodule
