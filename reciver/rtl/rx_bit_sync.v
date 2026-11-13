// Receiver bit synchroniser (digital clock/data recovery).
// Input : rx_in  = sliced output of the 433.92 MHz FSK demodulator (NRZ, 4.096 kbps)
// Clock : any clk with CLKS_PER_BIT cycles per tag bit (default 16 -> 65.536 kHz)
// Every data edge re-aligns the bit phase; the bit is sampled mid-bit.
// The tag's 0xAA preamble gives 8 edges to lock before the sync word.
module rx_bit_sync #(parameter CLKS_PER_BIT = 16)(
    input      clk, rst_n, rx_in,
    output reg bit_valid,       // one-clock pulse, once per recovered bit
    output reg bit_data);

    localparam [15:0] HALF = CLKS_PER_BIT/2;
    localparam [15:0] LAST = CLKS_PER_BIT-1;

    reg s1, s2, s3;             // 2-FF synchroniser + edge history
    reg [15:0] phase;
    wire edge_det = s2 ^ s3;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s1<=0; s2<=0; s3<=0; phase<=0; bit_valid<=0; bit_data<=0;
        end else begin
            s1 <= rx_in; s2 <= s1; s3 <= s2;
            bit_valid <= 0;
            if (edge_det)            phase <= 16'd0;          // re-align on every edge
            else if (phase == LAST)  phase <= 16'd0;          // free-run through long runs
            else                     phase <= phase + 16'd1;
            if (!edge_det && phase == HALF) begin             // mid-bit sample
                bit_valid <= 1; bit_data <= s2;
            end
        end
    end
endmodule
