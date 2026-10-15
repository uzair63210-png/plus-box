// Time counter + time-sensor integrator + latched FRESH/AGING/CRITICAL stage.
// Stage only moves up; only 'clear' (authenticated NFC restart) resets it.
module stage_engine #(parameter SAMPLE_PERIOD = 60)(
    input clk, rst_n, tick, run, clear,
    input [31:0] expiry, aging_win, acc_mild, acc_severe,
    input [7:0] t_base, t_abs_max, dwell_limit, sensor_in,
    output reg [31:0] age, acc,
    output reg [1:0] stage,
    output reg stage_changed);

    reg [15:0] samp_cnt;
    reg [7:0]  dwell_cnt;

    wire sample_now = (samp_cnt == SAMPLE_PERIOD-1);
    wire [8:0]  excess   = (sensor_in > t_base) ? (sensor_in - t_base) : 9'd0;
    wire [32:0] acc_sum  = {1'b0, acc} + {24'd0, excess};
    wire [31:0] acc_sat  = acc_sum[32] ? 32'hFFFFFFFF : acc_sum[31:0];
    wire [31:0] acc_n    = sample_now ? acc_sat : acc;
    wire [31:0] age_n    = age + 1;
    wire [7:0]  dwell_up = (dwell_cnt == 8'hFF) ? dwell_cnt : dwell_cnt + 1;
    wire [7:0]  dwell_n  = !sample_now ? dwell_cnt :
                           (sensor_in >= t_abs_max) ? dwell_up : 8'd0;

    wire critical = ({1'b0,age_n} >= {1'b0,expiry}) |
                    (acc_n >= acc_severe) |
                    ((dwell_limit != 0) && (dwell_n >= dwell_limit));
    wire aging    = (({1'b0,age_n} + {1'b0,aging_win}) >= {1'b0,expiry}) |
                    (acc_n >= acc_mild);
    wire [1:0] new_stage = critical ? 2'd2 : aging ? 2'd1 : 2'd0;


    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            age<=0; acc<=0; stage<=0; stage_changed<=0; samp_cnt<=0; dwell_cnt<=0;
        end else begin
            stage_changed <= 0;
            if (clear) begin
                age<=0; acc<=0; stage<=0; samp_cnt<=0; dwell_cnt<=0;
            end else if (tick && run) begin
                age <= age_n; acc <= acc_n; dwell_cnt <= dwell_n;
                samp_cnt <= sample_now ? 16'd0 : samp_cnt + 1;
                if (new_stage > stage) begin
                    stage <= new_stage; stage_changed <= 1;
                end
            end
        end
    end
endmodule