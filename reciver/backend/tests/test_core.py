import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from pulse_rx.protocol import *
from pulse_rx.rules import RuleEngine
from pulse_rx.service import Pipeline
from pulse_rx import shelf_life as sl
from pulse_rx.simulator import SimTag, cold_chain, with_excursion, run


def rtl_crc(payload56_bits):           # literal transcription of packet_tx.v crc16()
    c = 0xFFFF
    for i in range(55, -1, -1):
        d = (payload56_bits >> i) & 1
        c = ((c << 1) & 0xFFFF) ^ 0x1021 if ((c >> 15) & 1) ^ d else (c << 1) & 0xFFFF
    return c


class TestProtocol(unittest.TestCase):
    def test_crc_check_value(self):
        self.assertEqual(crc16_ccitt(b"123456789"), 0x29B1)    # CRC-16/CCITT-FALSE check value

    def test_matches_rtl_algorithm_and_layout(self):
        p = Packet(0xB0A00001, 2, 1, True, False, False, 30, 77)
        fr = build_frame(p)
        payload = int.from_bytes(fr[3:10], "big")
        self.assertEqual(int.from_bytes(fr[10:12], "big"), rtl_crc(payload))
        self.assertEqual(fr[:3], bytes([0xAA, 0x2D, 0xD4]))
        self.assertEqual(len(fr) * 8, 96)
        self.assertEqual(parse_frame(fr), p)
        self.assertEqual(p.status_byte, 0b10_001_1_0_0)

    def test_corruption_rejected(self):
        fr = bytearray(build_frame(Packet(1, 0, 0, False, False, False, 5, 1)))
        for bit in range(24, 96):                    # any single-bit error in payload/CRC
            b = bytearray(fr); b[bit // 8] ^= 1 << (7 - bit % 8)
            with self.assertRaises(FrameError):
                parse_frame(bytes(b))

    def test_host_record_roundtrip(self):
        p = Packet(0xDEADBEEF, 1, 2, False, True, False, 21, 9, rssi=88)
        hr = parse_host_record(build_host_record(6, p, gap=3))
        self.assertEqual((hr.event_name, hr.gap, hr.packet), ("QUERY", 3, p))
        bad = bytearray(build_host_record(6, p)); bad[3] ^= 1
        with self.assertRaises(FrameError):
            parse_host_record(bytes(bad))


def pk(seq, stage=0, sample=5, ack=False, query=False, batt=False, id_=1):
    return Packet(id_, stage, 1, batt, query, ack, sample, seq)


class TestRules(unittest.TestCase):
    def test_lifecycle(self):
        r = RuleEngine()
        self.assertEqual(r.process(pk(0), 0).kind, "new")
        self.assertFalse(r.process(pk(0), 1).accepted)                      # duplicate
        o = r.process(pk(3), 10); self.assertEqual((o.kind, o.gap), ("heartbeat", 2))
        o = r.process(pk(4, stage=1), 20); self.assertEqual(o.alerts[0].kind, "AGING")
        o = r.process(pk(5, stage=2), 30); self.assertEqual(o.alerts[0].kind, "CRITICAL")
        o = r.process(pk(6, stage=2), 40); self.assertEqual(o.kind, "alert_repeat")
        o = r.process(pk(7, stage=0), 50); self.assertEqual(o.alerts[0].kind, "ANOMALY")   # never backwards
        self.assertEqual(r.tags[1].stage, 2)
        o = r.process(pk(8, stage=2, query=True), 60); self.assertEqual(o.kind, "query")

    def test_ack_vs_restart(self):
        r = RuleEngine()
        r.process(pk(0, ack=True, sample=0), 0)
        self.assertEqual(r.process(pk(1, ack=True, sample=0), 1).kind, "ack")
        o = r.process(pk(2, ack=True, sample=1), 2); self.assertEqual(o.kind, "restart")
        r.process(pk(3, stage=2), 3); self.assertEqual(r.tags[1].stage, 2)
        o = r.process(pk(4, ack=True, sample=2), 4); self.assertEqual(o.kind, "restart")
        self.assertEqual(r.tags[1].stage, 0)                                  # authenticated restart resets
        self.assertEqual(r.process(pk(5, stage=0), 5).kind, "heartbeat")      # and no ANOMALY afterwards

    def test_lost_and_recovered(self):
        r = RuleEngine(lost_after_s=100)
        r.process(pk(0), 0)
        self.assertEqual(r.sweep(99), [])
        a = r.sweep(100); self.assertEqual([x.kind for x in a], ["LOST"])
        self.assertEqual(r.sweep(500), [])                                    # only once
        o = r.process(pk(1), 600); self.assertIn("RECOVERED", [x.kind for x in o.alerts])


class TestLostAndSecurity(unittest.TestCase):
    def test_alert_mode_tag_lost_after_30_min(self):
        r = RuleEngine()
        r.process(pk(0), 0); r.process(pk(1, stage=2), 100)
        self.assertEqual(r.sweep(100 + 1799), [])
        a = r.sweep(100 + 1800)
        self.assertEqual(len(a), 1); self.assertEqual(a[0].severity, "critical"); self.assertIn("alert mode", a[0].detail)
        r2 = RuleEngine(); r2.process(pk(0, id_=2), 0)                       # FRESH tags keep the 12 h rule
        self.assertEqual(r2.sweep(7200), []); self.assertEqual(len(r2.sweep(43800)), 1)

    def test_api_key(self):
        from pulse_rx.security import key_ok
        self.assertTrue(key_ok(None, None)); self.assertTrue(key_ok("k", "k"))
        self.assertFalse(key_ok(None, "k")); self.assertFalse(key_ok("x", "k")); self.assertFalse(key_ok("", "k"))


class TestML(unittest.TestCase):
    def test_model_beats_physics_baseline(self):
        Xtr, ytr = sl.synth_dataset(6000, seed=1)
        Xte, yte = sl.synth_dataset(2000, seed=99)          # different seed = unseen profiles
        m = sl.ShelfLifeModel().fit(Xtr, ytr)
        ml = np.abs(m.predict_frac(Xte) - yte).mean()
        phys = np.abs(m._phys_frac(Xte) - yte).mean()
        print(f"\n   held-out MAE (fraction of nominal life): ML={ml:.5f}  physics-only={phys:.5f}  "
              f"({100*(1-ml/phys):.0f}% lower)")
        self.assertLess(ml, phys)

    def test_hotter_box_has_less_life(self):
        m = sl.ShelfLifeModel().fit_synthetic(2500)
        cool = m.predict_hours([4.0] * 20, "dairy"); hot = m.predict_hours([4.0] * 10 + [25.0] * 10, "dairy")
        self.assertGreater(cool, hot)


class TestEndToEnd(unittest.TestCase):
    def test_fleet(self):
        pl = Pipeline()
        tags = [
            SimTag(0xA0000001, cold_chain()),                                              # healthy
            SimTag(0xA0000002, with_excursion(cold_chain(), 20 * 3600, 8 * 3600, 42)),     # 8 h at 42 C
            SimTag(0xA0000003, cold_chain(), die_at=30 * 3600),                            # dies
            SimTag(0xA0000004, cold_chain(7.0, 0.5)),
        ]
        sent, got = run(pl, tags, 4 * 86400, loss=0.01)
        kinds = {(a["box_id"], a["kind"]) for a in pl.store.alerts(1000)}
        self.assertIn((0xA0000002, "CRITICAL"), kinds)
        self.assertIn((0xA0000003, "LOST"), kinds)
        self.assertNotIn((0xA0000001, "CRITICAL"), kinds)
        self.assertEqual(pl.rules.tags[0xA0000002].stage, 2)
        rank = pl.dispatch_ranking()
        self.assertTrue(rank[0]["box_id"].endswith("A0000002") or rank[0]["action"].startswith("QUARANTINE"))
        self.assertEqual(rank[1]["action"].split()[0], "LOCATE")
        fefo = [r for r in rank if r["action"].startswith("DISPATCH")]
        self.assertEqual(fefo, sorted(fefo, key=lambda r: r["remaining_h"]))
        # warmer cold room (7 C) must rank ahead of 4 C box in FEFO
        self.assertTrue(fefo[0]["box_id"].endswith("A0000004"))
        print(f"\n   {sent} frames sent, {got} delivered, stats={pl.stats}")
        print("   ranking:", [(r['box_id'][-2:], r['action'].split()[0], r['remaining_days']) for r in rank])

    def test_bad_crc_dropped(self):
        pl = Pipeline(train_if_missing=False)
        fr = bytearray(build_frame(pk(0))); fr[8] ^= 4
        self.assertFalse(pl.ingest_raw(bytes(fr), 0)["accepted"])
        self.assertEqual(pl.stats["raw_bad_crc"], 1)
        self.assertEqual(pl.store.tags(), [])


class TestPersistenceAndBridge(unittest.TestCase):
    def test_restore_after_restart(self):
        import tempfile
        from pulse_rx.db import Store
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        pl = Pipeline(Store("sqlite:///" + path), train_if_missing=False)
        for i in range(3):
            pl.ingest_packet(pk(i, stage=0, sample=5 + i), 1000 + i * 21600)
        pl.ingest_packet(pk(3, stage=2, sample=40), 1000 + 3 * 21600)
        pl2 = Pipeline(Store("sqlite:///" + path), train_if_missing=False)
        self.assertEqual(pl2.restore(), 1)
        self.assertEqual(pl2.rules.tags[1].stage, 2)
        o = pl2.ingest_packet(pk(3, stage=2, sample=40), 5000)          # same seq as before the restart
        self.assertFalse(o["accepted"])                                  # dedupe survives restart
        self.assertEqual(len(pl2._series[1]), 4)

    def test_bridge_resync(self):
        from bridge import records
        recs = [build_host_record(2, Packet(0x7E7E7E7E, 0, 1, False, False, False, 0x7E, 0x7E), 0),   # 0x7E everywhere
                build_host_record(3, Packet(0xB0A00001, 2, 1, False, False, False, 30, 5), 1)]
        data = bytearray(b"\x00\x13\x7e\x99" + recs[0] + b"\xff" + recs[1])           # garbage between records
        it = iter([bytes(data), b""]); out = []
        gen = records(lambda: next(it, b""))
        out.append(next(gen)); out.append(next(gen))
        self.assertEqual(out, recs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
