// Builds and serialises a 96-bit frame, MSB first, NRZ.
// {preamble 8, sync 16, ID 32, status 8, sample 8, seq 8, CRC16 16}
// status = {stage[1:0], sensor_type[2:0], batt_low, query_flag, ack_flag}
module packet_tx #(parameter BIT_DIV = 8)(   // 32768/8 = 4096 bps
    input clk, rst_n, start,
    input [31:0] box_id, input [1:0] stage, input [2:0] sensor_type,
    input batt_low, input query_flag, input ack_flag, input [7:0] sample,
    output tx_en, output tx_data, output reg [7:0] seq);

    function [15:0] crc16;               // CRC-16/CCITT-FALSE, 7 payload bytes
        input [55:0] d;
        integer i; reg [15:0] c;
        begin
            c = 16'hFFFF;
            for (i = 55; i >= 0; i = i - 1)
                if (c[15] ^ d[i]) c = {c[14:0],1'b0} ^ 16'h1021;
                else              c = {c[14:0],1'b0};
            crc16 = c;
        end
    endfunction

    wire [7:0]  status  = {stage, sensor_type, batt_low, query_flag, ack_flag};
    wire [55:0] payload = {box_id, status, sample, seq};
    wire [95:0] frame   = {8'hAA, 16'h2DD4, payload, crc16(payload)};

    reg [95:0] shreg; reg busy; reg [6:0] bitcnt; reg [3:0] divcnt;
    assign tx_en   = busy;
    assign tx_data = busy ? shreg[95] : 1'b0;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin shreg<=0; busy<=0; bitcnt<=0; divcnt<=0; seq<=0; end
        else if (!busy) begin
            if (start) begin shreg<=frame; busy<=1; bitcnt<=0; divcnt<=0; end
        end else if (divcnt == BIT_DIV-1) begin
            divcnt <= 0; shreg <= {shreg[94:0],1'b0}; bitcnt <= bitcnt + 1;
            if (bitcnt == 95) begin busy <= 0; seq <= seq + 1; end
        end else divcnt <= divcnt + 1;
    end
endmodule