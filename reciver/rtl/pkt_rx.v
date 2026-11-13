// Frame hunter + decoder for the PULSE-Box uplink frame (MSB first):
//   {preamble 8 = 0xAA, sync 16 = 0x2DD4, ID 32, status 8, sample 8, seq 8, CRC16 16}
// status = {stage[1:0], sensor_type[2:0], batt_low, query_flag, ack_flag}
// CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflect, no xorout) over the
// 56 payload bits. Running the CRC over payload+CRC gives 0 for a good frame.
//
// False-lock protection (an FSK demodulator with no carrier outputs random bits):
//  * frame start needs the last 4 preamble bits + the 16-bit sync word (20-bit pattern, 1 bit error allowed)
//  * while receiving a body, an EXACT 24-bit {0xAA,0x2DD4} match restarts the frame, so a false lock
//    in noise can never swallow a real frame that follows it
module pkt_rx #(parameter [23:0] PATTERN = 24'hAA2DD4, parameter SYNC_TOL = 1)(
    input clk, rst_n, bit_valid, bit_data,
    output reg        sync_hit,     // pulse: sync word found
    output reg        pkt_valid,    // pulse: CRC-good frame, fields below valid
    output reg        crc_err,      // pulse: frame body received but CRC bad
    output reg [31:0] box_id,
    output reg [7:0]  status, sample, seq);

    localparam HUNT = 1'b0, BODY = 1'b1;

    reg        state;
    reg [23:0] sh;
    reg [6:0]  cnt;
    reg [15:0] crc;
    reg [55:0] pay;

    function [4:0] pop20;
        input [19:0] v;
        integer i;
        begin
            pop20 = 5'd0;
            for (i = 0; i < 20; i = i + 1) pop20 = pop20 + v[i];
        end
    endfunction

    wire [23:0] sh_n       = {sh[22:0], bit_data};
    wire        sync_ok    = (pop20(sh_n[19:0] ^ PATTERN[19:0]) <= SYNC_TOL);
    wire        exact_hit  = (sh_n == PATTERN);
    wire        fb       = crc[15] ^ bit_data;
    wire [15:0] crc_n    = fb ? ({crc[14:0],1'b0} ^ 16'h1021) : {crc[14:0],1'b0};
    wire [55:0] pay_n    = {pay[54:0], bit_data};

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state<=HUNT; sh<=24'd0; cnt<=0; crc<=16'hFFFF; pay<=0;
            sync_hit<=0; pkt_valid<=0; crc_err<=0;
            box_id<=0; status<=0; sample<=0; seq<=0;
        end else begin
            sync_hit<=0; pkt_valid<=0; crc_err<=0;
            if (bit_valid) begin
                if (state == HUNT) begin
                    sh <= sh_n;
                    if (sync_ok) begin
                        state<=BODY; cnt<=0; crc<=16'hFFFF; sync_hit<=1;
                    end
                end else begin
                    sh <= sh_n;
                    if (exact_hit) begin                       // false lock earlier: restart on the real frame
                        cnt <= 0; crc <= 16'hFFFF; sync_hit <= 1;
                    end else begin
                    crc <= crc_n;
                    if (cnt < 7'd56) pay <= pay_n;
                    if (cnt == 7'd71) begin                    // last CRC bit
                        state <= HUNT; sh <= 24'h000000;
                        if (crc_n == 16'h0000) begin
                            pkt_valid <= 1;
                            box_id <= pay[55:24]; status <= pay[23:16];
                            sample <= pay[15:8];  seq    <= pay[7:0];
                        end else crc_err <= 1;
                    end else cnt <= cnt + 7'd1;
                    end
                end
            end
        end
    end
endmodule
