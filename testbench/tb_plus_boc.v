`timescale 1ns/1ps
module tb;
    localparam PRESCALE = 128;            // 1 "second" = 128 clocks (fast sim)
    localparam WLT = 8;                   // write-link short interval (clocks)
    localparam [31:0] PW = 32'hC0FFEE01;
    reg clk = 0; always #5 clk = ~clk;
    reg rst_n = 0, wl_in = 0, uv_in = 0, batt_low = 0; reg [7:0] sensor_in = 8;
    wire tx_en, tx_data, irq;

    pulse_tag_core #(.PRESCALE(PRESCALE), .SAMPLE_PERIOD(5), .HB_PERIOD(20),
        .HB_JIT(16'h0003), .ALERT_MIN(3), .ALERT_MAX(12), .ALERT_JIT(16'h0001),
        .BIT_DIV(1), .UV_WINDOW(2*PRESCALE), .UV_LOCKOUT(10), .WL_T(WLT), .FAIL_LOCK(20))
      dut (.clk(clk), .rst_n(rst_n), .wl_in(wl_in), .uv_in(uv_in), .sensor_in(sensor_in),
           .batt_low(batt_low), .tx_en(tx_en), .tx_data(tx_data), .irq(irq));

    localparam A_ID=0, A_EXP=1, A_AGEWIN=2, A_MILD=3, A_SEV=4, A_CFG=5, A_PWD=6, A_CMD=8;

    integer pass=0, fail=0, crc_err=0, npk=0, qcount=0, ackcount=0, hbcount=0, a0;
    reg [7:0] last_ack_cnt = 0; reg [1:0] last_ack_stage = 3;

    function [15:0] crc68(input [67:0] d);
        integer i; reg [15:0] c;
        begin c = 16'hFFFF;
            for (i=67;i>=0;i=i-1) if (c[15]^d[i]) c={c[14:0],1'b0}^16'h1021; else c={c[14:0],1'b0};
            crc68 = c; end
    endfunction
    function [15:0] crc56(input [55:0] d);
        integer i; reg [15:0] c;
        begin c = 16'hFFFF;
            for (i=55;i>=0;i=i-1) if (c[15]^d[i]) c={c[14:0],1'b0}^16'h1021; else c={c[14:0],1'b0};
            crc56 = c; end
    endfunction

    // ---- write-link programmer model: pulse-interval coding ----
    task wl_pulse(input integer interval);
        begin wl_in=1; repeat(4) @(negedge clk); wl_in=0; repeat(interval-4) @(negedge clk); end
    endtask
    task wl_send(input [3:0] a, input [31:0] d, input [31:0] p, input bad_crc);
        reg [91:0] f; integer i;
        begin
            f = {8'hA5, a, d, p, crc68({a,d,p}) ^ (bad_crc ? 16'h0001 : 16'h0000)};
            repeat (6*WLT) @(negedge clk);               // idle gap
            for (i=91;i>=0;i=i-1) wl_pulse(f[i] ? 2*WLT : WLT);
            wl_pulse(WLT);                               // terminating rise
        end
    endtask
    task uv_burst(input integer k);
        integer i;
        begin for (i=0;i<k;i=i+1) begin uv_in=1; repeat(10) @(posedge clk); uv_in=0; repeat(10) @(posedge clk); end end
    endtask
    task wait_s(input integer s); begin repeat (s*PRESCALE) @(posedge clk); end endtask
    task check(input ok, input [8*52-1:0] name);
        begin if (ok) begin pass=pass+1; $display("  PASS: %0s", name); end
              else    begin fail=fail+1; $display("  FAIL: %0s", name); end end
    endtask

    // ---- FSK-side receiver model: decode every frame ----
    reg [95:0] rx; integer n = 0;
    always @(posedge clk) begin
        if (tx_en) begin rx = {rx[94:0], tx_data}; n = n + 1; end
        else if (n != 0) begin
            npk = npk + 1;
            if (n != 96 || rx[95:72] != 24'hAA2DD4 || rx[15:0] != crc56(rx[71:16])) crc_err = crc_err + 1;
            if (rx[33]) qcount = qcount + 1;
            if (rx[32]) begin ackcount = ackcount + 1; last_ack_cnt = rx[31:24]; last_ack_stage = rx[39:38]; end
            if (!rx[33] && !rx[32]) hbcount = hbcount + 1;
            $display("t=%4ds  PKT ID=%h stage=%0d q=%b ack=%b sample/cnt=%0d seq=%0d",
                $time/(10*PRESCALE), rx[71:40], rx[39:38], rx[33], rx[32], rx[31:24], rx[23:16]);
            n = 0;
        end
    end

    initial begin
        #100 rst_n = 1; repeat(20) @(negedge clk);
        $display("== Factory programming over write-only link (default password 0) ==");
        wl_send(A_ID,     32'hB0A00001, 32'h0, 0);
        wl_send(A_EXP,    32'd300,      32'h0, 0);
        wl_send(A_AGEWIN, 32'd100,      32'h0, 0);
        wl_send(A_MILD,   32'd40,       32'h0, 0);
        wl_send(A_SEV,    32'd100,      32'h0, 0);
        wl_send(A_CFG,    32'h0001FF0A, 32'h0, 0);   // base=10, sensor_type=1
        wl_send(A_PWD,    PW,           32'h0, 0);   // set real password
        wl_send(A_CMD,    32'd1,        PW,    0);   // start
        wait_s(2);
        check(ackcount == 8, "all 8 programming frames acknowledged");

        $display("== Test 1: cool box, heartbeats only ==");
        wait_s(50);
        check(dut.stage == 0, "stays FRESH while cool");
        check(hbcount >= 2,   "heartbeats received");

        $display("== Test 2: heat excursion ==");
        sensor_in = 30; wait_s(40);
        check(dut.stage == 2, "reaches CRITICAL from temperature integral");
        check(irq == 1,       "irq asserted");

        $display("== Test 3: write-link security ==");
        a0 = ackcount;
        wl_send(A_CMD, 32'd1, 32'hDEADBEEF, 0);   // wrong password
        wait_s(1);
        check(dut.stage == 2 && ackcount == a0, "wrong password dropped, no ack");
        wl_send(A_CMD, 32'd1, PW, 0);             // right password but inside lockout
        wait_s(1);
        check(dut.stage == 2 && ackcount == a0, "lockout blocks even the right password");
        wait_s(20);
        wl_send(A_CMD, 32'd1, PW, 1);             // corrupted CRC
        wait_s(1);
        check(dut.stage == 2 && ackcount == a0, "bad-CRC frame dropped");

        $display("== Test 4: authenticated reset + new shelf life (expiry 60, window 20) ==");
        wl_send(A_EXP,    32'd60, PW, 0);
        wl_send(A_AGEWIN, 32'd20, PW, 0);
        sensor_in = 8;
        wl_send(A_CMD,    32'd1,  PW, 0);
        wait_s(1);
        check(dut.stage == 0, "stage cleared by authenticated reset");
        wait_s(2);
        check(last_ack_cnt == 2 && last_ack_stage == 0, "ack packet reports restart count 2, stage FRESH");
        wait_s(45);
        check(dut.stage == 1, "AGING from date window");
        wait_s(25);
        check(dut.stage == 2, "CRITICAL at expiry");

        $display("== Test 5: UV query ==");
        uv_in = 1; wait_s(4); uv_in = 0; wait_s(1);
        check(qcount == 0, "steady light (sunlight) gives no query");
        uv_burst(1); wait_s(4);
        check(qcount == 0, "single pulse gives no query");
        uv_burst(3); wait_s(2);
        check(qcount == 1, "3-pulse UV code gives one status packet");
        uv_burst(3); wait_s(2);
        check(qcount == 1, "second code ignored during lockout");
        wait_s(12); uv_burst(3); wait_s(2);
        check(qcount == 2, "query accepted again after lockout");

        check(crc_err == 0, "all FSK packets CRC-valid");
        $display("== %0d passed, %0d failed, %0d packets ==", pass, fail, npk);
        $finish;
    end
    initial begin #400000000 $display("TIMEOUT"); $finish; end
    initial begin $dumpfile("tb_pulse_tag.vcd"); $dumpvars(0, tb); end
endmodule
