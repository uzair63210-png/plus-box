-- PostgreSQL schema for the PULSE-Box receiver system  (psql -f schema.sql)
CREATE TABLE IF NOT EXISTS tags (
    box_id       BIGINT PRIMARY KEY,
    product      TEXT,
    stage        SMALLINT NOT NULL DEFAULT 0,          -- 0 FRESH, 1 AGING, 2 CRITICAL (latched)
    last_seen    DOUBLE PRECISION,
    last_sample  SMALLINT,
    lost         SMALLINT NOT NULL DEFAULT 0,
    batt_low     SMALLINT NOT NULL DEFAULT 0,
    pkts         BIGINT   NOT NULL DEFAULT 0,
    missed       BIGINT   NOT NULL DEFAULT 0,          -- from seq gaps
    wr_count     SMALLINT,                             -- tag write-counter (restart audit trail)
    last_seq     SMALLINT,
    first_seen   DOUBLE PRECISION,
    stage_since  DOUBLE PRECISION,
    sensor_type  SMALLINT,
    rssi         SMALLINT
);
CREATE TABLE IF NOT EXISTS packets (
    id       BIGSERIAL PRIMARY KEY,
    ts       DOUBLE PRECISION NOT NULL,
    box_id   BIGINT NOT NULL REFERENCES tags(box_id) DEFERRABLE INITIALLY DEFERRED,
    stage    SMALLINT, sample SMALLINT, temp_c REAL, seq SMALLINT,
    ack SMALLINT, query SMALLINT, batt_low SMALLINT, rssi SMALLINT,
    kind TEXT, gap SMALLINT
);
CREATE INDEX IF NOT EXISTS ix_packets_box_ts ON packets(box_id, ts);
CREATE TABLE IF NOT EXISTS alerts (
    id BIGSERIAL PRIMARY KEY, ts DOUBLE PRECISION NOT NULL, box_id BIGINT NOT NULL,
    kind TEXT NOT NULL, severity TEXT NOT NULL, detail TEXT, acknowledged SMALLINT DEFAULT 0
);                                                      -- retained as dispute evidence
CREATE TABLE IF NOT EXISTS predictions (
    id BIGSERIAL PRIMARY KEY, ts DOUBLE PRECISION NOT NULL, box_id BIGINT NOT NULL,
    remaining_h REAL, product TEXT, model TEXT
);

CREATE TABLE IF NOT EXISTS locations (box_id BIGINT PRIMARY KEY, gateway TEXT, ts DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS tg_subscribers (chat_id BIGINT PRIMARY KEY, name TEXT, ts DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS tg_state (k TEXT PRIMARY KEY, v TEXT);
