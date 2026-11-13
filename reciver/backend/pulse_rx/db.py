"""Storage layer. SQLite for dev/tests, PostgreSQL in production (DATABASE_URL=postgresql://...).
Schema: backend/schema.sql (PostgreSQL). Same logical tables are created for SQLite."""
import os
import sqlite3
import threading

SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS tags(box_id INTEGER PRIMARY KEY, product TEXT, stage INTEGER, last_seen REAL,
    last_sample INTEGER, lost INTEGER, batt_low INTEGER, pkts INTEGER, missed INTEGER, wr_count INTEGER,
    last_seq INTEGER, first_seen REAL, stage_since REAL, sensor_type INTEGER, rssi INTEGER);
CREATE TABLE IF NOT EXISTS packets(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, box_id INTEGER, stage INTEGER,
    sample INTEGER, temp_c REAL, seq INTEGER, ack INTEGER, query INTEGER, batt_low INTEGER, rssi INTEGER,
    kind TEXT, gap INTEGER);
CREATE INDEX IF NOT EXISTS ix_packets_box_ts ON packets(box_id, ts);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, box_id INTEGER, kind TEXT,
    severity TEXT, detail TEXT, acknowledged INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS locations(box_id INTEGER PRIMARY KEY, gateway TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS tg_subscribers(chat_id INTEGER PRIMARY KEY, name TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS tg_state(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS predictions(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, box_id INTEGER,
    remaining_h REAL, product TEXT, model TEXT);
"""


class Store:
    def __init__(self, url=None):
        url = url or os.environ.get("DATABASE_URL", "sqlite:///pulse_rx.db")
        self.pg = url.startswith("postgres")
        self.lock = threading.RLock()
        if self.pg:
            import psycopg2, psycopg2.extras  # noqa
            self.conn = psycopg2.connect(url)
            self.conn.autocommit = True
        else:
            path = url.replace("sqlite:///", "")
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            with self.lock:
                self.conn.executescript(SQLITE_DDL)

    def _q(self, sql):
        return sql.replace("?", "%s") if self.pg else sql

    def execute(self, sql, args=()):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(self._q(sql), args)
            if not self.pg:
                self.conn.commit()
            return cur

    def rows(self, sql, args=()):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(self._q(sql), args)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    # --- writes
    def save_tag(self, s, product):
        self.execute("DELETE FROM tags WHERE box_id=?", (s.box_id,))
        self.execute("INSERT INTO tags VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (s.box_id, product, s.stage, s.last_seen, s.last_sample, int(s.lost), int(s.batt_low),
                      s.pkts, s.missed, s.wr_count, s.last_seq, s.first_seen, s.stage_since,
                      s.sensor_type, s.rssi))

    def add_packet(self, ts, p, temp_c, kind, gap):
        self.execute("INSERT INTO packets(ts,box_id,stage,sample,temp_c,seq,ack,query,batt_low,rssi,kind,gap)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (ts, p.box_id, p.stage, p.sample, temp_c, p.seq, int(p.ack), int(p.query),
                      int(p.batt_low), p.rssi, kind, gap))

    def add_alert(self, a):
        self.execute("INSERT INTO alerts(ts,box_id,kind,severity,detail) VALUES(?,?,?,?,?)",
                     (a.ts, a.box_id, a.kind, a.severity, a.detail))

    def add_prediction(self, ts, box_id, hours, product, model):
        self.execute("INSERT INTO predictions(ts,box_id,remaining_h,product,model) VALUES(?,?,?,?,?)",
                     (ts, box_id, hours, product, model))

    # --- reads
    def tags(self):
        return self.rows("SELECT * FROM tags ORDER BY box_id")

    def series(self, box_id, limit=500):
        return self.rows("SELECT ts,temp_c,stage,kind FROM packets WHERE box_id=? AND ack=0 "
                         "ORDER BY ts DESC LIMIT ?", (box_id, limit))[::-1]

    def alerts(self, limit=200):
        return self.rows("SELECT * FROM alerts ORDER BY ts DESC, id DESC LIMIT ?", (limit,))

    def latest_predictions(self):
        return self.rows("SELECT p.* FROM predictions p JOIN (SELECT box_id, MAX(id) mid FROM predictions "
                         "GROUP BY box_id) m ON p.id=m.mid")

    # --- location / telegram helpers
    def set_location(self, box_id, gateway, ts):
        self.execute("DELETE FROM locations WHERE box_id=?", (box_id,))
        self.execute("INSERT INTO locations VALUES(?,?,?)", (box_id, gateway, ts))

    def get_location(self, box_id):
        r = self.rows("SELECT gateway, ts FROM locations WHERE box_id=?", (box_id,))
        return r[0] if r else None

    def add_subscriber(self, chat_id, name, ts):
        self.execute("DELETE FROM tg_subscribers WHERE chat_id=?", (chat_id,))
        self.execute("INSERT INTO tg_subscribers VALUES(?,?,?)", (chat_id, name, ts))

    def remove_subscriber(self, chat_id):
        self.execute("DELETE FROM tg_subscribers WHERE chat_id=?", (chat_id,))

    def subscribers(self):
        return [r["chat_id"] for r in self.rows("SELECT chat_id FROM tg_subscribers")]

    def get_state(self, k, default=None):
        r = self.rows("SELECT v FROM tg_state WHERE k=?", (k,))
        return r[0]["v"] if r else default

    def set_state(self, k, v):
        self.execute("DELETE FROM tg_state WHERE k=?", (k,))
        self.execute("INSERT INTO tg_state VALUES(?,?)", (k, str(v)))

    def max_alert_id(self):
        return self.rows("SELECT COALESCE(MAX(id),0) m FROM alerts")[0]["m"]

    def alerts_after(self, last_id):
        return self.rows("SELECT * FROM alerts WHERE id>? ORDER BY id", (last_id,))

    def tag(self, box_id):
        r = self.rows("SELECT * FROM tags WHERE box_id=?", (box_id,))
        return r[0] if r else None

    def last_reading(self, box_id):
        r = self.rows("SELECT ts,temp_c,rssi FROM packets WHERE box_id=? AND ack=0 ORDER BY id DESC LIMIT 1", (box_id,))
        return r[0] if r else None

    def pred(self, box_id):
        r = self.rows("SELECT remaining_h, product, model, ts FROM predictions WHERE box_id=? ORDER BY id DESC LIMIT 1", (box_id,))
        return r[0] if r else None
