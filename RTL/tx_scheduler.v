// Decides WHEN to transmit.
//  FRESH   : one packet every HB_PERIOD s (+ LFSR jitter)
//  AGING / CRITICAL : immediate packet on stage change, then repeat with
//            exponential back-off ALERT_MIN -> ALERT_MAX (+ jitter)
module tx_scheduler #(
    parameter HB_PERIOD = 21600, parameter [15:0] HB_JIT = 16'h00FF,
    parameter ALERT_MIN = 10, ALERT_MAX = 300, parameter [15:0] ALERT_JIT = 16'h0007)(
    input clk, rst_n, tick, run, clear, stage_changed, query, ack,
    input [1:0] stage, input [31:0] seed,
    output reg tx_start, output reg tx_is_query, output reg tx_is_ack);

    reg [15:0] lfsr;
    reg [31:0] timer;
    reg [15:0] alert_int;
    wire fb = lfsr[15] ^ lfsr[13] ^ lfsr[12] ^ lfsr[10];
    wire [15:0] alert_dbl = {alert_int[14:0], 1'b0};

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            lfsr<=16'hACE1; timer<=0; alert_int<=ALERT_MIN; tx_start<=0; tx_is_query<=0; tx_is_ack<=0;
        end else begin
            tx_start <= 0; tx_is_query <= 0; tx_is_ack <= 0;
            lfsr <= {lfsr[14:0], fb};
            if (clear) begin
                lfsr      <= (seed[15:0] ^ seed[31:16]) | 16'h0001; // per-box seed
                timer     <= 32'd2 + (lfsr & HB_JIT);               // first packet soon
                alert_int <= ALERT_MIN;
            end else if (stage_changed) begin
                alert_int <= ALERT_MIN;
                timer     <= 32'd1 + (lfsr & ALERT_JIT);            // immediate alert
            end else if (tick && run) begin
                if (timer == 0) begin
                    tx_start <= 1;
                    if (stage == 0)
                        timer <= HB_PERIOD + (lfsr & HB_JIT);
                    else begin
                        timer     <= alert_int + (lfsr & ALERT_JIT);
                        alert_int <= (alert_dbl > ALERT_MAX) ? ALERT_MAX[15:0] : alert_dbl;
                    end
                end else timer <= timer - 1;
            end
            // on-demand status report (UV query); does not disturb the timer
            if (query && run) begin tx_start <= 1; tx_is_query <= 1; end
            // acknowledgment of a successful write-link frame (works before run too)
            if (ack) begin tx_start <= 1; tx_is_ack <= 1; end
        end
    end
endmodule