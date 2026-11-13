// Minimal 8N1 UART transmitter.
module uart_tx #(parameter CLKS_PER_BAUD = 57)(    // 6.5536 MHz / 115200 = 56.9
    input clk, rst_n, start, input [7:0] data,
    output reg txd, output reg busy);
    reg [8:0]  sh;
    reg [3:0]  n;
    reg [15:0] c;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin txd<=1; busy<=0; sh<=9'h1FF; n<=0; c<=0; end
        else if (!busy) begin
            if (start) begin sh<={1'b1,data}; txd<=0; busy<=1; c<=0; n<=0; end
        end else if (c == CLKS_PER_BAUD-1) begin
            c <= 0;
            if (n == 4'd9) begin busy<=0; txd<=1; end
            else begin txd<=sh[0]; sh<={1'b1,sh[8:1]}; n<=n+4'd1; end
        end else c <= c + 16'd1;
    end
endmodule
