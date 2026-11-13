// Per-tag state table + event generator (rule-based risk checks in hardware).
//  * drops duplicate frames (same box_id and seq as last accepted)
//  * separates config-write ACKs from authenticated RESTARTs (write-counter changed)
//  * tracks latched stage per tag (stage never goes backwards, except after an
//    authenticated restart which the tag announces with ack_flag)
//  * counts missed packets from seq gaps (collision statistics)
//  * flags a tag LOST when nothing is heard for LOST_TIMEOUT seconds
//    (default 2 x (6 h heartbeat + max jitter))
// status = {stage[1:0], sensor_type[2:0], batt_low, query_flag, ack_flag}
module tag_table #(
    parameter N            = 16,
    parameter SEC_DIV      = 6553600,    // clk cycles per second (6.5536 MHz clk)
    parameter LOST_TIMEOUT = 43800)(     // seconds
    input clk, rst_n,
    input pkt_valid, input [31:0] pkt_id, input [7:0] pkt_status, pkt_sample, pkt_seq,
    output reg        evt_valid,
    output reg [3:0]  evt_code,
    output reg [31:0] evt_id,
    output reg [7:0]  evt_status, evt_sample, evt_seq, evt_gap,
    output reg [7:0]  cnt_tags, cnt_lost,
    output reg [15:0] cnt_dup);

    localparam E_NONE=4'd0, E_NEW=4'd1, E_HB=4'd2, E_STAGE_UP=4'd3, E_REPEAT=4'd4,
               E_RESTART=4'd5, E_QUERY=4'd6, E_LOST=4'd7, E_RECOVER=4'd8,
               E_ANOMALY=4'd9, E_FULL=4'd10, E_ACK=4'd11;

    localparam S_IDLE=2'd0, S_LOOK=2'd1, S_UPD=2'd2;

    reg [31:0] t_id   [0:N-1];
    reg [7:0]  t_seq  [0:N-1];
    reg [7:0]  t_stat [0:N-1];   // last status byte seen
    reg [1:0]  t_stage[0:N-1];   // highest stage seen (latched)
    reg [7:0]  t_wr   [0:N-1];   // last write-counter (restart count) seen in an ack
    reg [31:0] t_seen [0:N-1];
    reg        t_valid[0:N-1];
    reg        t_lost [0:N-1];

    reg [1:0]  st;
    reg [31:0] now, sec_cnt;
    reg [31:0] l_id; reg [7:0] l_status, l_sample, l_seq;
    reg        r_found, r_free; reg [4:0] r_slot, r_freeslot;
    reg [4:0]  ptr;
    integer    i;

    // temporaries (blocking)
    reg f_found, f_free; reg [4:0] f_slot, f_freeslot;
    reg [1:0] lstage; reg [3:0] code; reg was_lost; reg [7:0] gap;

    initial begin
        for (i = 0; i < N; i = i + 1) begin
            t_valid[i]=0; t_lost[i]=0; t_id[i]=0; t_seq[i]=0; t_stat[i]=0;
            t_stage[i]=0; t_seen[i]=0; t_wr[i]=0;
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            st<=S_IDLE; now<=0; sec_cnt<=0; ptr<=0;
            evt_valid<=0; evt_code<=0; evt_id<=0; evt_status<=0; evt_sample<=0;
            evt_seq<=0; evt_gap<=0; cnt_tags<=0; cnt_lost<=0; cnt_dup<=0;
            l_id<=0; l_status<=0; l_sample<=0; l_seq<=0;
            r_found<=0; r_free<=0; r_slot<=0; r_freeslot<=0;
            for (i = 0; i < N; i = i + 1) begin t_valid[i]<=0; t_lost[i]<=0; end
        end else begin
            evt_valid <= 0;
            // 1 Hz time base
            if (sec_cnt == SEC_DIV-1) begin sec_cnt <= 0; now <= now + 32'd1; end
            else sec_cnt <= sec_cnt + 32'd1;

            case (st)
            S_IDLE: begin
                if (pkt_valid) begin
                    l_id<=pkt_id; l_status<=pkt_status; l_sample<=pkt_sample; l_seq<=pkt_seq;
                    st <= S_LOOK;
                end else begin
                    // lost-tag scanner: one slot per idle clock
                    if (t_valid[ptr] && !t_lost[ptr] && ((now - t_seen[ptr]) >= LOST_TIMEOUT)) begin
                        t_lost[ptr] <= 1;
                        cnt_lost <= cnt_lost + 8'd1;
                        evt_valid<=1; evt_code<=E_LOST; evt_id<=t_id[ptr];
                        evt_status<=t_stat[ptr]; evt_sample<=0; evt_seq<=t_seq[ptr]; evt_gap<=0;
                    end
                    ptr <= (ptr == N-1) ? 5'd0 : ptr + 5'd1;
                end
            end

            S_LOOK: begin   // find the entry for l_id and the first free slot
                f_found = 0; f_free = 0; f_slot = 0; f_freeslot = 0;
                for (i = N-1; i >= 0; i = i - 1) begin
                    if (!t_valid[i]) begin f_free = 1; f_freeslot = i; end
                    if (t_valid[i] && t_id[i] == l_id) begin f_found = 1; f_slot = i; end
                end
                r_found <= f_found; r_free <= f_free; r_slot <= f_slot; r_freeslot <= f_freeslot;
                st <= S_UPD;
            end

            S_UPD: begin
                st <= S_IDLE;
                lstage = l_status[7:6];
                evt_id<=l_id; evt_status<=l_status; evt_sample<=l_sample; evt_seq<=l_seq; evt_gap<=0;
                if (!r_found) begin
                    if (r_free) begin
                        t_valid[r_freeslot]<=1; t_lost[r_freeslot]<=0; t_id[r_freeslot]<=l_id;
                        t_seq[r_freeslot]<=l_seq; t_stat[r_freeslot]<=l_status;
                        t_stage[r_freeslot]<=lstage; t_seen[r_freeslot]<=now;
                        t_wr[r_freeslot]<=l_status[0] ? l_sample : 8'd0;
                        cnt_tags <= cnt_tags + 8'd1;
                        evt_valid<=1; evt_code<=E_NEW;
                    end else begin
                        evt_valid<=1; evt_code<=E_FULL;
                    end
                end else if (l_seq == t_seq[r_slot]) begin
                    cnt_dup <= cnt_dup + 16'd1;                // duplicate: drop silently
                end else begin
                    gap      = l_seq - t_seq[r_slot] - 8'd1;   // packets missed (collisions)
                    was_lost = t_lost[r_slot];
                    if (l_status[0]) begin                      // ack packet: sample = write-counter
                        if (l_sample != t_wr[r_slot]) begin     // counter moved => authenticated restart
                            code = E_RESTART;
                            t_stage[r_slot] <= lstage; t_wr[r_slot] <= l_sample;
                        end else code = E_ACK;                  // ordinary config-write acknowledgment
                    end else begin
                        if (lstage > t_stage[r_slot]) begin
                            code = E_STAGE_UP; t_stage[r_slot] <= lstage;
                        end else if (lstage < t_stage[r_slot]) code = E_ANOMALY;
                        else if (lstage == 2'd0) code = E_HB;
                        else code = E_REPEAT;
                        if (l_status[1] && (code == E_HB || code == E_REPEAT)) code = E_QUERY;
                        if (was_lost && (code == E_HB || code == E_REPEAT || code == E_QUERY))
                            code = E_RECOVER;
                    end
                    if (was_lost) cnt_lost <= cnt_lost - 8'd1;
                    t_seq[r_slot]<=l_seq; t_stat[r_slot]<=l_status; t_seen[r_slot]<=now;
                    t_lost[r_slot]<=0;
                    evt_valid<=1; evt_code<=code; evt_gap<=gap;
                end
            end
            default: st <= S_IDLE;
            endcase
        end
    end
endmodule
