// PULSE-Box tag: digital core (top level)
module pulse_tag_core #(
    parameter PRESCALE=32768, SAMPLE_PERIOD=60,
    parameter HB_PERIOD=21600, parameter [15:0] HB_JIT=16'h00FF,
    parameter ALERT_MIN=10, ALERT_MAX=300, parameter [15:0] ALERT_JIT=16'h0007,
    parameter BIT_DIV=8,
    parameter UV_WINDOW=65536, UV_LOCKOUT=60,
    parameter WL_T=16, FAIL_LOCK=5)(
    input clk, rst_n,
    input wl_in,                      // write-only link: coil envelope comparator
    input uv_in,                      // UV-A photodiode comparator
    input [7:0] sensor_in, input batt_low,
    output tx_en, output tx_data,     // to FSK modulator
    output irq);                      // stage != FRESH

    wire tick, run, clear, stage_changed, tx_start, query, ack, tx_is_query, tx_is_ack;
    wire wl_wr; wire [3:0] wl_addr; wire [31:0] wl_data, wl_pwd;
    wire [31:0] box_id, expiry, aging_win, acc_mild, acc_severe, age, acc;
    wire [7:0]  t_base, t_abs_max, dwell_limit, seq, wr_count;
    wire [2:0]  sensor_type;
    wire [1:0]  stage;

    assign irq = (stage != 2'd0);

    tick_gen #(.PRESCALE(PRESCALE)) u_tick (.clk(clk), .rst_n(rst_n), .tick(tick));

    uv_query #(.WINDOW(UV_WINDOW), .LOCKOUT(UV_LOCKOUT)) u_uv (.clk(clk), .rst_n(rst_n),
        .tick(tick), .uv_in(uv_in), .query(query));

    wlink_rx #(.T(WL_T)) u_wl (.clk(clk), .rst_n(rst_n), .wl_in(wl_in),
        .wr(wl_wr), .addr(wl_addr), .data(wl_data), .pwd(wl_pwd));

    wlink_regfile #(.FAIL_LOCK(FAIL_LOCK)) u_regs (.clk(clk), .rst_n(rst_n), .tick(tick),
        .wl_wr(wl_wr), .wl_addr(wl_addr), .wl_data(wl_data), .wl_pwd(wl_pwd),
        .box_id(box_id), .expiry(expiry), .aging_win(aging_win), .acc_mild(acc_mild),
        .acc_severe(acc_severe), .t_base(t_base), .t_abs_max(t_abs_max),
        .dwell_limit(dwell_limit), .sensor_type(sensor_type), .run(run), .clear(clear),
        .ack(ack), .wr_count(wr_count));

    stage_engine #(.SAMPLE_PERIOD(SAMPLE_PERIOD)) u_stage (.clk(clk), .rst_n(rst_n),
        .tick(tick), .run(run), .clear(clear), .expiry(expiry), .aging_win(aging_win),
        .acc_mild(acc_mild), .acc_severe(acc_severe), .t_base(t_base),
        .t_abs_max(t_abs_max), .dwell_limit(dwell_limit), .sensor_in(sensor_in),
        .age(age), .acc(acc), .stage(stage), .stage_changed(stage_changed));

    tx_scheduler #(.HB_PERIOD(HB_PERIOD), .HB_JIT(HB_JIT), .ALERT_MIN(ALERT_MIN),
        .ALERT_MAX(ALERT_MAX), .ALERT_JIT(ALERT_JIT)) u_sched (.clk(clk), .rst_n(rst_n),
        .tick(tick), .run(run), .clear(clear), .stage_changed(stage_changed),
        .query(query), .ack(ack), .stage(stage), .seed(box_id),
        .tx_start(tx_start), .tx_is_query(tx_is_query), .tx_is_ack(tx_is_ack));

    // ack packets carry the restart counter in the 'sample' byte
    packet_tx #(.BIT_DIV(BIT_DIV)) u_pkt (.clk(clk), .rst_n(rst_n), .start(tx_start),
        .box_id(box_id), .stage(stage), .sensor_type(sensor_type), .batt_low(batt_low),
        .query_flag(tx_is_query), .ack_flag(tx_is_ack),
        .sample(tx_is_ack ? wr_count : sensor_in),
        .tx_en(tx_en), .tx_data(tx_data), .seq(seq));
endmodule