module uart_rx(
  input logic clk,
  input logic rst_n,
  input logic serial_i,
  output logic start_seen
);

always_ff @(posedge clk or negedge rst_n) begin
  if (!rst_n) begin
    start_seen <= 1'b0;
  end else if (!serial_i) begin
    start_seen <= 1'b1;
  end
end

endmodule
