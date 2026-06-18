module fifo_lite(
  input logic clk,
  input logic rst_n,
  input logic push_i,
  input logic pop_i,
  input logic [7:0] data_i,
  output logic [7:0] data_o,
  output logic full_o,
  output logic empty_o
);

logic [1:0] fill_level;

always_ff @(posedge clk or negedge rst_n) begin
  if (!rst_n) begin
    fill_level <= 2'd0;
    data_o <= 8'd0;
  end else begin
    if (push_i && !full_o) begin
      fill_level <= fill_level + 2'd1;
      data_o <= data_i;
    end
    if (pop_i && !empty_o) begin
      fill_level <= fill_level - 2'd1;
    end
  end
end

assign full_o = (fill_level == 2'd3);
assign empty_o = (fill_level == 2'd0);

endmodule
