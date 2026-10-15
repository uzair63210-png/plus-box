// UV/optical query detector.
// uv_in = output of a comparator on a UV-A photodiode (asynchronous).
// A query is accepted only for PULSES rising edges within WINDOW clocks,
// so steady sunlight / room light never wakes the transmitter.
// After a query, input is ignored for LOCKOUT seconds (battery protection).
module uv_query #(parameter PULSES = 3, WINDOW = 65536, LOCKOUT = 60)(
    input clk, rst_n, tick, uv_in,
    output reg query);

    reg s1, s2, s3;                       // 2-FF synchroniser + edge memory
    wire rise = s2 & ~s3;
    reg [2:0]  cnt;
    reg [16:0] win;
    reg [7:0]  lock;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s1<=0; s2<=0; s3<=0; cnt<=0; win<=0; lock<=0; query<=0;
        end else begin
            s1<=uv_in; s2<=s1; s3<=s2; query<=0;
            if (lock != 0) begin
                if (tick) lock <= lock - 1;
                cnt <= 0; win <= 0;
            end else if (cnt != 0 && win == WINDOW-1) begin
                cnt <= 0; win <= 0;                       // window expired
            end else begin
                if (cnt != 0) win <= win + 1;
                if (rise) begin
                    if (cnt == PULSES-1) begin
                        query <= 1; cnt <= 0; win <= 0; lock <= LOCKOUT;
                    end else cnt <= cnt + 1;
                end
            end
        end
    end
endmodule