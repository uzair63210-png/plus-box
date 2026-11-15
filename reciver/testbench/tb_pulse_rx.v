`timescale 1ns/1ps
// System testbench: REAL tag (pulse_tag_core from the plus-box repo) -> receiver.
// Tag and receiver run on ASYNCHRONOUS clocks (receiver 16x oversampled, +0.3 % offset)
// so the bit synchroniser is genuinely exercised.
// Compile (from repo root):
//   iverilog -g2005 -o rx_sim RTL/*.v  <plus-box>/RTL/*.v  testbench/tb_pulse_rx.v && vvp rx_sim
// NOTE: the plus-box file 'tick gen.v' has a space in its name; quote it in the command.
module tb_pulse_rx;
    localparam PRESCALE = 128;            // tag: 1 "second" = 128 tag clocks
    localparam WLT = 8;
    localparam [31:0] PW = 32'hC0FFEE01, ID = 32'hB0A00001;
    localparam A_ID=0, A_EXP=1, A_AGEWIN=2, A_MILD=3, A_SEV=4, A_CFG=5, A_PWD=6, A_CMD=8;

    // ---------------- tag side ----------------
    reg tclk = 0; always #80 tclk = ~tclk;                 // 160 ns -> 1 tag bit (BIT_DIV=1)
    reg rst_n = 0, wl_in = 0, uv_in = 0, batt_low = 0; reg [7:0] sensor_in = 8;
    wire tx_en, tx_data, irq;
    pulse_tag_core #(.PRESCALE(PRESCALE), .SAMPLE_PERIOD(5), .HB_PERIOD(20),
        .HB_JIT(16'h0003), .ALERT_MIN(3), .ALERT_MAX(12), .ALERT_JIT(16'h0001),
        .BIT_DIV(1), .UV_WINDOW(2*PRESCALE), .UV_LOCKOUT(10), .WL_T(WLT), .FAIL_LOCK(20))
      tag (.clk(tclk), .rst_n(rst_n), .wl_in(wl_in), .uv_in(uv_in), .sensor_in(sensor_in),
           .batt_low(batt_low), .tx_en(tx_en), .tx_data(tx_data), .irq(irq));

    // ---------------- channel ----------------
    reg connected = 1, flip = 0;
    wire rx_data = (tx_data & connected) ^ flip;

    // ---------------- receiver ----------------
    reg rclk = 0; always #5.015 rclk = ~rclk;              // 10.03 ns period
    wire uart_txd, fifo_overflow, led;
    wire [15:0] n_ok, n_crc, n_dup; wire [7:0] n_tags, n_lost;
    pulse_rx_top #(.CLKS_PER_BIT(16), .SEC_DIV(2042), .LOST_TIMEOUT(50), .LOST_TIMEOUT_ALERT(30), .N_TAGS(4),
                   .CLKS_PER_BAUD(16)) rx (
        .clk(rclk), .rst_n(rst_n), .rx_data(rx_data), .rx_cd(1'b1), .rssi_in(8'd77),
        .uart_txd(uart_txd), .cnt_pkt_ok(n_ok), .cnt_crc_err(n_crc), .cnt_tags(n_tags),
        .cnt_lost(n_lost), .cnt_dup(n_dup), .fifo_overflow(fifo_overflow), .led_activity(led));

    // ---------------- helpers (from the tag testbench) ----------------
    function [15:0] crc68(input [67:0] d);
        integer i; reg [15:0] c;
        begin c = 16'hFFFF;
            for (i=67;i>=0;i=i-1) if (c[15]^d[i]) c={c[14:0],1'b0}^16'h1021; else c={c[14:0],1'b0};
            crc68 = c; end
    endfunction
    task wl_pulse(input integer interval);
        begin wl_in=1; repeat(4) @(negedge tclk); wl_in=0; repeat(interval-4) @(negedge tclk); end
    endtask
    task wl_send(input [3:0] a, input [31:0] d, input [31:0] p);
        reg [91:0] f; integer i;
        begin
            f = {8'hA5, a, d, p, crc68({a,d,p})};
            repeat (6*WLT) @(negedge tclk);
            for (i=91;i>=0;i=i-1) wl_pulse(f[i] ? 2*WLT : WLT);
            wl_pulse(WLT);
        end
    endtask
    task wait_s(input integer s); begin repeat (s*PRESCALE) @(posedge tclk); end endtask
    task uv_burst(input integer k);
        integer i;
        begin for (i=0;i<k;i=i+1) begin uv_in=1; repeat(10) @(posedge tclk); uv_in=0; repeat(10) @(posedge tclk); end end
    endtask
    integer pass=0, fail=0;
    task check(input ok, input [8*56-1:0] name);
        begin if (ok) begin pass=pass+1; $display("  PASS: %0s", name); end
              else    begin fail=fail+1; $display("  FAIL: %0s", name); end end
    endtask

    // ---------------- tag-side packet counter (reference) ----------------
    integer tag_pkts = 0; reg tx_en_d = 0;
    always @(posedge tclk) begin tx_en_d <= tx_en; if (tx_en_d && !tx_en) tag_pkts = tag_pkts + 1; end

    // ---------------- UART record decoder ----------------
    reg [7:0] ub; integer bi, rn = 0, chk_err = 0, any_evt = 0, k;
    reg [7:0] rec [0:11];
    integer ev_cnt [0:15];
    reg [31:0] first_id = 0; reg [3:0] first_code = 0; reg [7:0] restart_cnt = 0;
    reg [7:0] last_rssi = 0; reg [7:0] chk;
    initial for (k=0;k<16;k=k+1) ev_cnt[k] = 0;
    always @(negedge uart_txd) begin : uart_rx
        repeat(8) @(posedge rclk);
        for (bi=0;bi<8;bi=bi+1) begin repeat(16) @(posedge rclk); ub[bi] = uart_txd; end
        repeat(16) @(posedge rclk);
        if (rn == 0) begin if (ub == 8'h7E) rn = 1; end
        else begin
            rec[rn] = ub; rn = rn + 1;
            if (rn == 12) begin
                chk = 0; for (k=1;k<=10;k=k+1) chk = chk ^ rec[k];
                if (chk != rec[11]) chk_err = chk_err + 1;
                ev_cnt[rec[1][3:0]] = ev_cnt[rec[1][3:0]] + 1;
                if (any_evt == 0) begin first_id = {rec[2],rec[3],rec[4],rec[5]}; first_code = rec[1][3:0]; end
                any_evt = any_evt + 1; last_rssi = rec[10];
                if (rec[1][3:0] == 4'd5) restart_cnt = rec[7];
                $display("t=%7t EVT code=%0d id=%h stage=%0d sample=%0d seq=%0d gap=%0d rssi=%0d",
                    $time, rec[1][3:0], {rec[2],rec[3],rec[4],rec[5]}, rec[6][7:6], rec[7], rec[8], rec[9], rec[10]);
                rn = 0;
            end
        end
    end

    // ---------------- stimulus ----------------
    integer ok0, err0, lost0;
    initial begin
        #200 rst_n = 1; repeat(20) @(negedge tclk);
        $display("== program tag over write-only link ==");
        wl_send(A_ID,ID,0); wl_send(A_EXP,300,0); wl_send(A_AGEWIN,100,0);
        wl_send(A_MILD,40,0); wl_send(A_SEV,100,0); wl_send(A_CFG,32'h0001FF0A,0);
        wl_send(A_PWD,PW,0); wl_send(A_CMD,1,PW);
        wait_s(2);
        check(n_tags == 1 && first_code == 4'd1 && first_id == ID, "first frame registers a NEW tag with correct 32-bit ID");

        $display("== cool box: heartbeats ==");
        wait_s(50);
        check(ev_cnt[2] >= 2, "heartbeat events reach the host");
        check(last_rssi == 77, "RSSI latched at sync is forwarded");

        $display("== heat excursion ==");
        sensor_in = 30; wait_s(40);
        check(ev_cnt[3] >= 1, "STAGE_UP event raised on excursion");
        check(ev_cnt[4] >= 1, "alert repeats are reported (back-off packets)");

        $display("== channel corruption ==");
        err0 = n_crc;
        @(posedge tx_en); repeat(40) @(posedge tclk);   // ~40 bits into the frame (1 bit = 1 tag clock)
        flip = 1; #160 flip = 0;
        wait_s(15);
        check(n_crc > err0, "bit error inside a frame is caught by CRC (dropped, counted)");

        $display("== authenticated restart ==");
        wl_send(A_EXP,60,PW); wl_send(A_AGEWIN,20,PW); sensor_in = 8; wl_send(A_CMD,1,PW);
        wait_s(3);
        check(ev_cnt[5] >= 1 && restart_cnt == 2, "RESTART event carries write-counter = 2");

        $display("== UV query ==");
        uv_burst(3); wait_s(3);
        check(ev_cnt[6] >= 1, "QUERY event forwarded");

        $display("== lost tag / recovery ==");
        lost0 = ev_cnt[7];
        connected = 0; wait_s(70);
        check(ev_cnt[7] == lost0 + 1 && n_lost == 1, "LOST event after timeout, lost counter = 1");
        connected = 1; wait_s(30);
        check(ev_cnt[8] >= 1 && n_lost == 0, "RECOVERED event when tag is heard again");

        check(ev_cnt[9] == 0, "no stage-regression ANOMALY on a healthy tag");
        check(chk_err == 0 && !fifo_overflow, "UART records intact, FIFO never overflowed");
        check(n_ok + n_crc >= tag_pkts - 1, "every tag packet was detected (ok or CRC-bad)");
        $display("== %0d passed, %0d failed | tag pkts=%0d rx ok=%0d crc_err=%0d dup=%0d ==",
                 pass, fail, tag_pkts, n_ok, n_crc, n_dup);
        $finish;
    end
    initial begin #600000000 $display("TIMEOUT"); $finish; end
endmodule
