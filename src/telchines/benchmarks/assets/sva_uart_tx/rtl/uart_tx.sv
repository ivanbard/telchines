module uart_tx(
  input logic clk,
  input logic rst_n,
  input logic [3:0] tx_fifo_level,
  output logic ready_o
);

always_ff @(posedge clk or negedge rst_n) begin
  if (!rst_n) begin
    ready_o <= 1'b0;
  end else begin
    ready_o <= (tx_fifo_level != 4'd0);
  end
end

endmodule
