// Event FIFO + UART record framer toward the Python gateway bridge.
// 12-byte record:
//  [0]=0x7E [1]=event [2..5]=box_id (MSB first) [6]=status [7]=sample [8]=seq
//  [9]=gap [10]=rssi [11]=XOR of bytes 1..10
module host_if #(parameter CLKS_PER_BAUD = 57)(
    input clk, rst_n,
    input evt_valid, input [3:0] evt_code, input [31:0] evt_id,
    input [7:0] evt_status, evt_sample, evt_seq, evt_gap, evt_rssi,
    output txd, output reg overflow);

    reg [79:0] mem [0:7];
    reg [2:0]  wp, rp; reg [3:0] count;
    reg [79:0] rec; reg [3:0] idx; reg sending; reg u_start; reg [7:0] u_data;
    wire u_busy;

    uart_tx #(.CLKS_PER_BAUD(CLKS_PER_BAUD)) u_uart (.clk(clk), .rst_n(rst_n),
        .start(u_start), .data(u_data), .txd(txd), .busy(u_busy));

    // record = {code8, id32, status, sample, seq, gap, rssi}
    wire [7:0] b1 = rec[79:72];
    wire [7:0] b2 = rec[71:64], b3 = rec[63:56], b4 = rec[55:48], b5 = rec[47:40];
    wire [7:0] b6 = rec[39:32], b7 = rec[31:24], b8 = rec[23:16], b9 = rec[15:8], b10 = rec[7:0];
    wire [7:0] chk = b1^b2^b3^b4^b5^b6^b7^b8^b9^b10;

    reg [7:0] cur;
    always @* begin
        case (idx)
            4'd0: cur = 8'h7E;  4'd1: cur = b1;  4'd2: cur = b2;  4'd3: cur = b3;
            4'd4: cur = b4;     4'd5: cur = b5;  4'd6: cur = b6;  4'd7: cur = b7;
            4'd8: cur = b8;     4'd9: cur = b9;  4'd10: cur = b10; default: cur = chk;
        endcase
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wp<=0; rp<=0; count<=0; sending<=0; idx<=0; u_start<=0; u_data<=0; rec<=0; overflow<=0;
        end else begin
            u_start <= 0;
            if (evt_valid) begin
                if (count == 4'd8) overflow <= 1;               // sticky: FIFO overrun
                else begin
                    mem[wp] <= {4'd0, evt_code, evt_id, evt_status, evt_sample, evt_seq, evt_gap, evt_rssi};
                    wp <= wp + 3'd1;
                end
            end
            // occupancy: push and pop may coincide
            if ((evt_valid && count != 4'd8) && !(!sending && count != 0)) count <= count + 4'd1;
            else if (!(evt_valid && count != 4'd8) && (!sending && count != 0)) count <= count - 4'd1;

            if (!sending) begin
                if (count != 0) begin
                    rec <= mem[rp]; rp <= rp + 3'd1; idx <= 0; sending <= 1;
                end
            end else if (!u_busy && !u_start) begin
                u_data <= cur; u_start <= 1;
                if (idx == 4'd11) begin sending <= 0; end else idx <= idx + 4'd1;
            end
        end
    end
endmodule
