// Write-only short-range link receiver (NFC-like, no NFC chip, no read-back).
// Input wl_in = envelope-detector/comparator output of a coil (async).
// Pulse-interval coding on rising edges (like RFID PIE):
//     interval ~ T  -> bit 0,   interval ~ 2T -> bit 1   (threshold 1.5T)
// so an RC-oscillator tolerance of +-25 % is harmless.
// Frame (92 bits, MSB first): header 0xA5 | addr[3:0] | data[31:0] | pwd[31:0] | CRC16
// An idle gap > 4T aborts/starts a frame.  Each frame is self-contained (stateless).
module wlink_rx #(parameter T = 16)(
    input clk, rst_n, wl_in,
    output reg wr, output reg [3:0] addr, output reg [31:0] data, output reg [31:0] pwd);

    localparam [9:0] THR_BIT = T + T/2;
    localparam [9:0] THR_GAP = 4*T;

    function [15:0] crc16;                // CRC-16/CCITT-FALSE over addr,data,pwd
        input [67:0] d;
        integer i; reg [15:0] c;
        begin
            c = 16'hFFFF;
            for (i = 67; i >= 0; i = i - 1)
                if (c[15] ^ d[i]) c = {c[14:0],1'b0} ^ 16'h1021;
                else              c = {c[14:0],1'b0};
            crc16 = c;
        end
    endfunction

    reg s1, s2, s3; wire rise = s2 & ~s3;
    reg [9:0]  gap;
    reg [6:0]  nbits;
    reg [91:0] sh;
    reg        active;

    wire        bitval = (gap >= THR_BIT);
    wire [91:0] full   = {sh[90:0], bitval};
    wire        hdr_ok = (full[91:84] == 8'hA5);
    wire        crc_ok = (full[15:0] == crc16(full[83:16]));

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s1<=0; s2<=0; s3<=0; gap<=0; nbits<=0; sh<=0; active<=0;
            wr<=0; addr<=0; data<=0; pwd<=0;
        end else begin
            s1<=wl_in; s2<=s1; s3<=s2; wr<=0;
            if (rise) begin
                gap <= 0;
                if (!active || gap > THR_GAP) begin active <= 1; nbits <= 0; end
                else begin
                    sh <= full; nbits <= nbits + 1;
                    if (nbits == 91) begin
                        active <= 0;
                        if (hdr_ok && crc_ok) begin
                            wr<=1; addr<=full[83:80]; data<=full[79:48]; pwd<=full[47:16];
                        end
                    end
                end
            end else begin
                if (gap != 10'h3FF) gap <= gap + 1;
                if (gap > THR_GAP) active <= 0;
            end
        end
    end
endmodule