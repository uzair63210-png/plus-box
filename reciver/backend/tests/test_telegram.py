import json, os, sys, tempfile, threading, unittest, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pulse_rx.protocol import Packet
from pulse_rx.service import Pipeline
from pulse_rx.telegram_bot import Notifier, Telegram

CALLS, QUEUE = [], []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        method = self.path.rsplit("/", 1)[1]
        if method == "getUpdates":
            res = [QUEUE.pop(0) for _ in range(len(QUEUE))]
        else:
            CALLS.append((method, body)); res = {"message_id": 1}
        out = json.dumps({"ok": True, "result": res}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)


def upd(i, chat, text):
    return {"update_id": i, "message": {"chat": {"id": chat, "first_name": "Uz"}, "text": text}}


GW = {"cold-room-1": {"name": "Pune Cold Room 1", "lat": 18.6298, "lon": 73.7997}}


class TestBot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls): cls.srv.shutdown()

    def setUp(self):
        CALLS.clear(); QUEUE.clear()
        from pulse_rx.db import Store
        self.pl = Pipeline(Store("sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")), train_if_missing=True)
        self.bot = Notifier(self.pl, tg=Telegram("TOKEN", self.base), gateways=GW, passcode="s3cret")

    def texts(self): return [b["text"] for m, b in CALLS if m == "sendMessage"]

    def test_stage_change_alerts_with_location_and_details(self):
        self.bot.init_cursor()
        QUEUE.append(upd(1, 111, "/start s3cret")); self.bot.poll_updates(timeout=0)
        self.assertIn("Subscribed", self.texts()[-1]); CALLS.clear()
        t0 = time.time()
        for i in range(6):                                                  # healthy heartbeats
            self.pl.ingest_packet(Packet(0xB0A00001, 0, 1, False, False, False, 5, i), t0 + i * 21600, "cold-room-1")
        self.assertEqual(self.bot.poll_alerts(), 0)                         # NEW_TAG not pushed by default
        self.pl.ingest_packet(Packet(0xB0A00001, 1, 1, False, False, False, 14, 6), t0 + 7 * 21600, "cold-room-1")
        self.pl.ingest_packet(Packet(0xB0A00001, 2, 1, False, False, False, 41, 7), t0 + 7 * 21600 + 60, "cold-room-1")
        self.assertEqual(self.bot.poll_alerts(), 2)
        msgs = self.texts()
        self.assertEqual(len(msgs), 2)
        aging, crit = msgs
        self.assertIn("FRESH -&gt; AGING" if "&gt;" in aging else "FRESH -> AGING", aging)
        self.assertIn("B0A00001", aging); self.assertIn("Remaining shelf life", aging)
        self.assertIn("Pune Cold Room 1", aging); self.assertIn("maps.google.com/?q=18.6298,73.7997", aging)
        self.assertIn("AGING -&gt; CRITICAL", crit.replace("-> ", "-&gt; ")) if "&gt;" in crit else self.assertIn("AGING -> CRITICAL", crit)
        self.assertIn("0 h - do not dispatch", crit); self.assertIn("41 °C", crit)
        locs = [b for m, b in CALLS if m == "sendLocation"]
        self.assertEqual(len(locs), 2)
        self.assertEqual((locs[0]["latitude"], locs[0]["longitude"]), (18.6298, 73.7997))
        self.assertEqual(self.bot.poll_alerts(), 0)                         # never sent twice
        print("\n----- sample CRITICAL message -----\n" + crit.replace("&lt;", "<").replace("&gt;", ">") + "\n-----")

    def test_commands_and_security(self):
        QUEUE.append(upd(1, 222, "/status")); self.bot.poll_updates(timeout=0)
        self.assertIn("Not authorised", self.texts()[-1])
        QUEUE.append(upd(2, 222, "/start wrong")); self.bot.poll_updates(timeout=0)
        self.assertIn("passcode", self.texts()[-1]); self.assertEqual(self.bot.store.subscribers(), [])
        QUEUE.append(upd(3, 222, "/start s3cret")); self.bot.poll_updates(timeout=0)
        self.assertEqual(self.bot.store.subscribers(), [222])
        t0 = time.time()
        for i, (bid, temp) in enumerate([(0xB0A00001, 4), (0xB0A00002, 7)]):
            for k in range(3):
                self.pl.ingest_packet(Packet(bid, 0, 1, False, False, False, temp, k), t0 + k * 21600, "cold-room-1")
        for cmd in ("/status", "/box B0A00002", "/where b0a00001", "/dispatch", "/alerts", "/box FFFF", "/stop"):
            CALLS.clear(); QUEUE.append(upd(10, 222, cmd)); self.bot.poll_updates(timeout=0)
            self.assertTrue(self.texts(), cmd)
            if cmd == "/status": self.assertIn("2 boxes", self.texts()[0])
            if cmd == "/box B0A00002": self.assertTrue(any(m == "sendLocation" for m, _ in CALLS))
            if cmd == "/dispatch": self.assertLess(self.texts()[0].index("B0A00002"), self.texts()[0].index("B0A00001"))  # warmer first
        self.assertEqual(self.bot.store.subscribers(), [])

    def test_lost_alert_has_last_location(self):
        self.bot.store.add_subscriber(5, "x", 0); self.bot.init_cursor()
        self.pl.ingest_packet(Packet(0xB0A00003, 0, 1, False, False, False, 5, 0), 1000.0, "cold-room-1")
        self.pl.sweep(1000.0 + 50000)
        self.assertEqual(self.bot.poll_alerts(), 1)
        self.assertIn("LOST", self.texts()[0]); self.assertIn("Pune Cold Room 1", self.texts()[0])
        self.assertTrue(any(m == "sendLocation" for m, _ in CALLS))


def cb(i, chat, data, name="Ravi"):
    return {"update_id": i, "callback_query": {"id": f"cb{i}", "from": {"id": chat, "first_name": name},
                                               "message": {"chat": {"id": chat}}, "data": data}}


class TestImprovements(TestBot):
    def make_critical(self, n, t0):
        for k in range(n):
            bid = 0xB0A10000 + k
            self.pl.ingest_packet(Packet(bid, 0, 1, False, False, False, 5, 0), t0, "cold-room-1")
            self.pl.ingest_packet(Packet(bid, 2, 1, False, False, False, 40, 1), t0 + 60, "cold-room-1")

    def test_burst_is_one_grouped_message(self):
        self.bot.store.add_subscriber(7, "x", 0); self.bot.init_cursor()
        self.make_critical(8, time.time())
        self.assertEqual(self.bot.poll_alerts(), 8)
        msgs = [(m, b) for m, b in CALLS if m == "sendMessage"]
        self.assertEqual(len(msgs), 1)                                   # not 8
        self.assertIn("8 boxes - CRITICAL", msgs[0][1]["text"])
        self.assertTrue(all(f"B0A1000{k}" in msgs[0][1]["text"] for k in range(8)))
        self.assertIn("reply_markup", msgs[0][1])
        self.assertEqual(sum(m == "sendLocation" for m, _ in CALLS), 1)  # one pin per receiver, not per box

    def test_escalation_then_acknowledge_stops_it(self):
        self.bot.store.add_subscriber(7, "x", 0); self.bot.store.add_subscriber(8, "y", 0); self.bot.init_cursor()
        t0 = time.time(); self.make_critical(1, t0)
        self.bot.poll_alerts(now=t0); CALLS.clear()
        self.assertEqual(self.bot.escalate(t0 + 10 * 60), 0)                   # too early
        self.assertEqual(self.bot.escalate(t0 + 16 * 60), 1)                   # 15 min -> escalation 1
        self.assertIn("ESCALATION 1/3", self.texts()[0]); self.assertEqual(len(self.texts()), 2)  # both subscribers
        alert_id = self.pl.store.alerts(1)[0]["id"]
        CALLS.clear(); QUEUE.append(cb(1, 7, f"ack:{alert_id}")); self.bot.poll_updates(timeout=0)
        self.assertTrue(self.pl.store.is_acked(alert_id))
        self.assertTrue(any(m == "answerCallbackQuery" for m, _ in CALLS))
        self.assertIn("acknowledged by <b>Ravi</b>", self.texts()[0])
        CALLS.clear()
        self.assertEqual(self.bot.escalate(t0 + 40 * 60), 0); self.assertEqual(self.texts(), [])  # stopped

    def test_escalation_caps_at_max(self):
        self.bot.store.add_subscriber(7, "x", 0); self.bot.init_cursor()
        t0 = time.time(); self.make_critical(1, t0); self.bot.poll_alerts(now=t0)
        n = sum(self.bot.escalate(t0 + k * 16 * 60) for k in range(1, 8))
        self.assertEqual(n, 3)

    def test_unauthorised_cannot_acknowledge(self):
        self.bot.init_cursor(); self.make_critical(1, time.time())
        aid = self.pl.store.alerts(1)[0]["id"]
        QUEUE.append(cb(1, 999, f"ack:{aid}")); self.bot.poll_updates(timeout=0)
        self.assertFalse(self.pl.store.is_acked(aid))
        self.assertEqual([b["text"] for m, b in CALLS if m == "answerCallbackQuery"], ["Not authorised"])

    def test_product_command_changes_remaining_life(self):
        self.bot.store.add_subscriber(9, "x", 0)
        t0 = time.time()
        for k in range(6):
            self.pl.ingest_packet(Packet(0xB0A00001, 0, 1, False, False, False, 6, k), t0 + k * 21600, "cold-room-1")
        before = self.pl.store.pred(0xB0A00001)["remaining_h"]
        QUEUE.append(upd(1, 9, "/product B0A00001 vaccine")); self.bot.poll_updates(timeout=0)
        self.assertIn("now <b>vaccine</b>", self.texts()[-1])
        after = self.pl.store.pred(0xB0A00001)
        self.assertEqual(after["product"], "vaccine"); self.assertGreater(after["remaining_h"], 5 * before)
        self.assertEqual(self.pl.product(0xB0A00001), "vaccine")            # survives later packets
        self.pl.ingest_packet(Packet(0xB0A00001, 0, 1, False, False, False, 6, 6), t0 + 7 * 21600)
        self.assertEqual(self.pl.store.tag(0xB0A00001)["product"], "vaccine")
        CALLS.clear(); QUEUE.append(upd(2, 9, "/product B0A00001 caviar")); self.bot.poll_updates(timeout=0)
        self.assertIn("Usage", self.texts()[-1])

    def test_daily_digest_once_per_day(self):
        self.bot.store.add_subscriber(7, "x", 0); self.bot.digest_hour = 8
        self.pl.ingest_packet(Packet(0xB0A00001, 0, 1, False, False, False, 5, 0), 1000.0, "cold-room-1")
        from datetime import datetime, timedelta, timezone
        tz = timezone(timedelta(minutes=330))
        d = lambda h, day: datetime(2026, 11, day, h, 0, tzinfo=tz).timestamp()
        self.assertFalse(self.bot.maybe_digest(d(7, 15)))
        self.assertTrue(self.bot.maybe_digest(d(8, 15)))
        self.assertFalse(self.bot.maybe_digest(d(9, 15)))
        self.assertTrue(self.bot.maybe_digest(d(8, 16)))
        self.assertIn("Daily digest", self.texts()[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
