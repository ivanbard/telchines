module counter(
  input logic clk,
  input logic rst_n,
  input logic enable_i,
  output logic [3:0] count_o
);

always_ff @(posedge clk or negedge rst_n) begin
  if (!rst_n) begin
    count_o <= 4'd0;
  end else if (enable_i) begin
    count_o <= count_o + 4'd1;
  end
end

endmodule
