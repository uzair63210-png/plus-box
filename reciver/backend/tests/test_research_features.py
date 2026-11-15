import json, os, sys, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from pulse_rx import shelf_life as sl
from pulse_rx.protocol import Packet
from pulse_rx.service import Pipeline
from pulse_rx.assistant import Assistant, TOOLS
from pulse_rx.db import Store

GW = {"cold-room-1": {"name": "Pune Cold Room 1", "lat": 18.6, "lon": 73.8}}
REQS, SCRIPT = [], []


class FakeAnthropic(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        REQS.append(({k.lower(): v for k, v in self.headers.items()}, body))
        out = json.dumps(SCRIPT.pop(0)).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)


def new_pl(train=False):
    return Pipeline(Store("sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")), train_if_missing=train)


class TestScience(unittest.TestCase):
    def test_mkt_matches_published_examples(self):
        self.assertAlmostEqual(sl.mkt_c([5] * 50 + [20] * 50), 15.4, delta=0.2)    # TechNet-21 worked example
        self.assertAlmostEqual(sl.mkt_c(np.linspace(4, 27, 200)), 18.0, delta=0.4)  # 6-day trip example
        self.assertAlmostEqual(sl.mkt_c([5.0] * 10), 5.0, places=6)                 # constant T -> MKT = T
        self.assertGreater(sl.mkt_c([4, 4, 4, 30]), np.mean([4, 4, 4, 30]))         # heat-weighted

    def test_dairy_parameters_reproduce_published_milk_data(self):
        nominal, q10 = sl.PRODUCTS["dairy"]
        ratio_5_to_10 = float(sl.rate(10, q10) / sl.rate(5, q10))      # literature: 30 d -> 12 d  (x2.5)
        self.assertTrue(2.0 < ratio_5_to_10 < 3.2, ratio_5_to_10)
        ratio_13_to_19 = float(sl.rate(19, q10) / sl.rate(13, q10))    # literature: 72 h -> 24 h (x3)
        self.assertTrue(2.0 < ratio_13_to_19 < 4.5, ratio_13_to_19)

    def test_sensor_calibration_can_represent_freezing(self):
        self.assertEqual(sl.decode_temp(8, 1), 8.0)
        self.assertEqual(sl.decode_temp(80, 2), 0.0); self.assertEqual(sl.decode_temp(79, 2), -0.5)
        self.assertEqual(sl.decode_temp(0, 2), -40.0)

    def test_interval_covers_most_heldout_cases(self):
        Xtr, ytr = sl.synth_dataset(4000, seed=1); Xte, yte = sl.synth_dataset(1500, seed=99)
        m = sl.ShelfLifeModel().fit(Xtr, ytr)
        lo, mid, hi = m.predict_range_frac(Xte)
        cov = float(np.mean((yte >= lo - 1e-9) & (yte <= hi + 1e-9)))
        print(f"\n   80% interval empirical coverage on held-out synthetic data: {cov:.2f}")
        self.assertTrue(0.70 <= cov <= 0.92, cov); self.assertTrue(np.all(lo <= mid) and np.all(mid <= hi))


class TestVaccineLimits(unittest.TestCase):
    def test_freeze_and_heat_alerts_once_per_episode(self):
        pl = new_pl(); t = 1000.0
        pl.product_by_tag[1] = "vaccine"
        def send(seq, temp_c):
            return pl.ingest_packet(Packet(1, 0, 2, False, False, False, int((temp_c + 40) / 0.5), seq), t + seq * 21600)
        send(0, 5.0)                                              # sensor_type 2: 0.5 C steps from -40 C
        pl.store.set_product(1, "vaccine")
        kinds = lambda: [a["kind"] for a in pl.store.alerts(50)]
        send(1, 5.0); self.assertEqual([k for k in kinds() if k in ("FREEZE", "HEAT_LIMIT")], [])
        send(2, -1.0); self.assertEqual(kinds().count("FREEZE"), 1)
        send(3, -2.0); self.assertEqual(kinds().count("FREEZE"), 1)       # same episode: no repeat
        send(4, 5.0); send(5, -1.5); self.assertEqual(kinds().count("FREEZE"), 2)   # new episode
        send(6, 22.0); self.assertEqual(kinds().count("HEAT_LIMIT"), 1)
        pl2 = new_pl(); pl2.ingest_packet(Packet(2, 0, 2, False, False, False, 0, 0), 1.0)   # dairy: -40 C, no vaccine rule
        self.assertNotIn("FREEZE", [a["kind"] for a in pl2.store.alerts(10)])


class TestAssistant(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropic)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls): cls.srv.shutdown()

    def setUp(self):
        REQS.clear(); SCRIPT.clear()
        self.pl = new_pl()
        for k in range(4):
            self.pl.ingest_packet(Packet(0xB0A00001, 0, 1, False, False, False, 5 + k, k), 1000 + k * 21600, "cold-room-1")
        self.ai = Assistant(self.pl, GW, api_key="test-key", base=self.base, per_hour=3)

    def test_tool_use_round_trip(self):
        SCRIPT.append({"stop_reason": "tool_use", "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "tu_1", "name": "get_box", "input": {"box_id": "B0A00001"}}]})
        SCRIPT.append({"stop_reason": "end_turn", "content": [{"type": "text", "text": "B0A00001 is FRESH at 8 C."}]})
        ans = self.ai.ask("how is box B0A00001?", chat_id=1)
        self.assertEqual(ans, "B0A00001 is FRESH at 8 C.")
        h, first = REQS[0]
        self.assertEqual(h["x-api-key"], "test-key"); self.assertEqual(h["anthropic-version"], "2023-06-01")
        self.assertEqual(first["model"], "claude-sonnet-5-5")
        self.assertEqual({t["name"] for t in first["tools"]}, {"list_boxes", "get_box", "get_alerts", "dispatch_ranking"})
        self.assertIn("read-only", first["system"])
        second = REQS[1][1]["messages"]
        self.assertEqual(second[1]["role"], "assistant")
        tr = second[2]["content"][0]
        self.assertEqual((tr["type"], tr["tool_use_id"]), ("tool_result", "tu_1"))
        data = json.loads(tr["content"])
        self.assertEqual(data["box_id"], "B0A00001"); self.assertEqual(data["location"], "Pune Cold Room 1")
        self.assertEqual(data["recent_temps_c"], [5.0, 6.0, 7.0, 8.0]); self.assertIsNotNone(data["mkt_c"])

    def test_tools_are_read_only_and_robust(self):
        before = self.pl.store.tag(0xB0A00001)
        for name, args in [("list_boxes", {}), ("get_alerts", {"limit": 3}), ("dispatch_ranking", {}),
                           ("get_box", {"box_id": "ZZZ"}), ("get_box", {"box_id": "FFFFFFFF"}), ("drop_tables", {})]:
            self.ai.run_tool(name, args)
        self.assertEqual(before, self.pl.store.tag(0xB0A00001))
        self.assertIn("error", self.ai.run_tool("get_box", {"box_id": "FFFFFFFF"}))
        self.assertIn("error", self.ai.run_tool("drop_tables", {}))
        self.assertEqual(self.ai.run_tool("list_boxes", {"location": "cold-room-1"})["count"], 1)
        self.assertEqual(self.ai.run_tool("list_boxes", {"stage": "CRITICAL"})["count"], 0)
        self.assertFalse(any(t["name"].startswith(("set_", "ack", "delete", "write")) for t in TOOLS))

    def test_rate_limit_and_missing_key_and_loop_cap(self):
        for _ in range(3):
            SCRIPT.append({"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]})
            self.assertEqual(self.ai.ask("q", chat_id=9), "ok")
        self.assertIn("Rate limit", self.ai.ask("q", chat_id=9))
        self.assertEqual(self.ai.ask("q", chat_id=10) if SCRIPT.append({"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}) is None else "", "ok")
        self.assertIn("not configured", Assistant(self.pl, GW, api_key="").ask("q"))
        for _ in range(5):                                         # model keeps asking for tools forever
            SCRIPT.append({"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "x", "name": "list_boxes", "input": {}}]})
        self.assertIn("tool-call limit", self.ai.ask("loop", chat_id=11, max_iters=5))

    def test_api_failure_is_graceful(self):
        bad = Assistant(self.pl, GW, api_key="k", base="http://127.0.0.1:9")      # nothing listening
        self.assertIn("unavailable", bad.ask("q", chat_id=1))


class TestGroupHint(unittest.TestCase):
    def test_shared_location_hint(self):
        from tests.test_telegram import Fake, CALLS, QUEUE   # reuse the fake Telegram server
        from pulse_rx.telegram_bot import Notifier, Telegram
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake); threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            CALLS.clear(); pl = new_pl()
            bot = Notifier(pl, tg=Telegram("T", f"http://127.0.0.1:{srv.server_port}"), gateways=GW, passcode="x")
            bot.store.add_subscriber(1, "a", 0); bot.init_cursor(); t0 = time.time()
            for k in range(5):
                pl.ingest_packet(Packet(0xB0A20000 + k, 0, 1, False, False, False, 5, 0), t0, "cold-room-1")
                pl.ingest_packet(Packet(0xB0A20000 + k, 2, 1, False, False, False, 40, 1), t0 + 60, "cold-room-1")
            bot.poll_alerts(now=t0 + 61)
            text = [b["text"] for m, b in CALLS if m == "sendMessage"][0]
            self.assertIn("All 5 boxes were heard at Pune Cold Room 1", text); self.assertIn("refrigeration", text)
            lines, _ = bot.fmt.box_lines(pl.store, 0xB0A20000, t0)
            self.assertTrue(any(l.startswith("MKT:") for l in lines))
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
