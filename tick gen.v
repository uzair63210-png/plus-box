// 32.768 kHz -> 1 Hz tick (PRESCALE = 32768)
module tick_gen #(parameter PRESCALE = 32768)(
    input clk, input rst_n, output reg tick);
    reg [15:0] cnt;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) begin cnt <= 0; tick <= 0; end
        else if (cnt == PRESCALE-1) begin cnt <= 0; tick <= 1; end
        else begin cnt <= cnt + 1; tick <= 0; end
endmodule