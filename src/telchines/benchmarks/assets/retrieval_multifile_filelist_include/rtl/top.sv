module top (
  input  logic clk,
  input  logic rst_n,
  input  logic uart_rx_i,
  output logic [7:0] rx_data_o,
  output logic rx_valid_o
);
  uart_core #(
    .BAUD_DIVISOR(16)
  ) u_uart_core (
    .clk(clk),
    .rst_n(rst_n),
    .serial_i(uart_rx_i),
    .data_o(rx_data_o),
    .valid_o(rx_valid_o)
  );
endmodule
