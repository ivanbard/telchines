module broken_counter(
  input logic clk,
  input logic rst_n,
  output logic [3:0] count
);

always_ff @(posedge clk or negedge rst_n) begin
  if (!rst_n) begin
    count <= 4'd0;
  end else begin
    count <= count + 1;
  end
end

endmodule
