// PULSE-Box receiver / gateway digital back-end (top level).
//   FSK demodulator slicer -> rx_bit_sync -> pkt_rx -> tag_table -> host_if (UART)
// Radio front-end (433.92 MHz FSK receiver IC/module, e.g. a sub-GHz transceiver
// in raw/direct mode) is outside this RTL: it supplies rx_data (+ optional
// carrier-detect and a quantised RSSI byte).
module pulse_rx_top #(
    parameter CLKS_PER_BIT   = 1600,     // clk cycles per 4.096 kbps bit (6.5536 MHz clk)
    parameter SEC_DIV        = 6553600,  // clk cycles per second
    parameter LOST_TIMEOUT   = 43800,    // s: 2 x (6 h + jitter)
    parameter N_TAGS         = 16,
    parameter CLKS_PER_BAUD  = 57)(      // 6.5536 MHz / 115200
    input  clk, rst_n,
    input  rx_data,                      // sliced FSK demod output
    input  rx_cd,                        // carrier detect (tie 1 if unused)
    input  [7:0] rssi_in,                // quantised RSSI (tie 0 if unused)
    output uart_txd,
    output [15:0] cnt_pkt_ok, cnt_crc_err,
    output [7:0]  cnt_tags, cnt_lost,
    output [15:0] cnt_dup,
    output fifo_overflow,
    output led_activity);

    wire bit_valid, bit_data, sync_hit, pkt_valid, crc_err;
    wire [31:0] box_id; wire [7:0] status, sample, seq;
    wire evt_valid; wire [3:0] evt_code; wire [31:0] evt_id;
    wire [7:0] evt_status, evt_sample, evt_seq, evt_gap;

    rx_bit_sync #(.CLKS_PER_BIT(CLKS_PER_BIT)) u_sync (.clk(clk), .rst_n(rst_n),
        .rx_in(rx_data), .bit_valid(bit_valid), .bit_data(bit_data));

    pkt_rx u_pkt (.clk(clk), .rst_n(rst_n), .bit_valid(bit_valid & rx_cd), .bit_data(bit_data),
        .sync_hit(sync_hit), .pkt_valid(pkt_valid), .crc_err(crc_err),
        .box_id(box_id), .status(status), .sample(sample), .seq(seq));

    // RSSI captured at sync-word time (signal is strongest/most stable there)
    reg [7:0] rssi_lat;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) rssi_lat <= 0; else if (sync_hit) rssi_lat <= rssi_in;

    tag_table #(.N(N_TAGS), .SEC_DIV(SEC_DIV), .LOST_TIMEOUT(LOST_TIMEOUT)) u_tbl (
        .clk(clk), .rst_n(rst_n),
        .pkt_valid(pkt_valid), .pkt_id(box_id), .pkt_status(status),
        .pkt_sample(sample), .pkt_seq(seq),
        .evt_valid(evt_valid), .evt_code(evt_code), .evt_id(evt_id),
        .evt_status(evt_status), .evt_sample(evt_sample), .evt_seq(evt_seq), .evt_gap(evt_gap),
        .cnt_tags(cnt_tags), .cnt_lost(cnt_lost), .cnt_dup(cnt_dup));

    host_if #(.CLKS_PER_BAUD(CLKS_PER_BAUD)) u_host (.clk(clk), .rst_n(rst_n),
        .evt_valid(evt_valid), .evt_code(evt_code), .evt_id(evt_id),
        .evt_status(evt_status), .evt_sample(evt_sample), .evt_seq(evt_seq),
        .evt_gap(evt_gap), .evt_rssi(rssi_lat), .txd(uart_txd), .overflow(fifo_overflow));

    reg [15:0] ok_c, err_c;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) begin ok_c<=0; err_c<=0; end
        else begin if (pkt_valid) ok_c<=ok_c+16'd1; if (crc_err) err_c<=err_c+16'd1; end
    assign cnt_pkt_ok = ok_c; assign cnt_crc_err = err_c;
    assign led_activity = pkt_valid | crc_err;
endmodule
