// Write-only register file. No read path exists, so ID, thresholds and password
// can never be read out through this link. Every frame carries the password.
// Wrong password -> frame dropped and the link is locked for FAIL_LOCK seconds
// (slows guessing without letting an attacker brick the tag).
// A successful write pulses 'ack' -> tag sends an acknowledgment packet.
module wlink_regfile #(parameter FAIL_LOCK = 5)(
    input clk, rst_n, tick,
    input wl_wr, input [3:0] wl_addr, input [31:0] wl_data, input [31:0] wl_pwd,
    output reg [31:0] box_id, expiry, aging_win, acc_mild, acc_severe,
    output [7:0] t_base, t_abs_max, dwell_limit, output [2:0] sensor_type,
    output reg run, clear, ack,
    output reg [7:0] wr_count);

    localparam A_ID=0, A_EXP=1, A_AGEWIN=2, A_MILD=3, A_SEV=4, A_CFG=5, A_PWD=6, A_CMD=8;

    reg [31:0] cfg, password;
    reg [7:0]  lock;

    assign t_base      = cfg[7:0];
    assign t_abs_max   = cfg[15:8];
    assign sensor_type = cfg[18:16];
    assign dwell_limit = cfg[27:20];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            box_id<=0; expiry<=0; aging_win<=32'd7776000;
            acc_mild<=32'hFFFFFFFF; acc_severe<=32'hFFFFFFFF;
            cfg<=32'h0000FF00; password<=0; lock<=0;
            wr_count<=0; run<=0; clear<=0; ack<=0;
        end else begin
            clear <= 0; ack <= 0;
            if (lock != 0 && tick) lock <= lock - 1;
            if (wl_wr && lock == 0) begin
                if (wl_pwd != password) lock <= FAIL_LOCK;
                else begin
                    ack <= 1;
                    case (wl_addr)
                        A_ID:     box_id     <= wl_data;
                        A_EXP:    expiry     <= wl_data;
                        A_AGEWIN: aging_win  <= wl_data;
                        A_MILD:   acc_mild   <= wl_data;
                        A_SEV:    acc_severe <= wl_data;
                        A_CFG:    cfg        <= wl_data;
                        A_PWD:    password   <= wl_data;
                        A_CMD: begin
                            if (wl_data[0]) begin run<=1; clear<=1; wr_count<=wr_count+1; end
                            if (wl_data[1]) run <= 0;
                        end
                        default: ack <= 0;
                    endcase
                end
            end
        end
    end
endmodule