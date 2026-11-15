"""Ingest pipeline: validate -> dedupe/rules -> persist -> ML prediction -> dispatch ranking."""
import time
from typing import List, Optional
from .protocol import Packet, parse_frame, parse_host_record, FrameError, STAGES
from .rules import RuleEngine, TagState
from .shelf_life import (ShelfLifeModel, decode_temp, PRODUCTS, SAMPLE_H, VACCINE_MAX_C, VACCINE_FREEZE_C)
from .rules import Alert
from .db import Store


class Pipeline:
    def __init__(self, store: Optional[Store] = None, model: Optional[ShelfLifeModel] = None,
                 default_product="dairy", product_by_tag=None, train_if_missing=True):
        self.store = store or Store("sqlite:///:memory:")
        self.rules = RuleEngine()
        self.product_by_tag = product_by_tag or {}
        self.default_product = default_product
        self.model = model
        if self.model is None:
            self.model = ShelfLifeModel()          # untrained = physics-only baseline
            if train_if_missing:
                try:
                    self.model.fit_synthetic(n=2500)
                except Exception:       # scikit-learn missing -> stays on the physics baseline
                    self.model.model = None
        self.stats = {"raw_ok": 0, "raw_bad_crc": 0, "duplicates": 0, "host_records": 0}
        self._series = {}                # box_id -> list of temps (heartbeat-rate)
        self._last_series_ts = {}
        self._limit_state = {}

    def restore(self):
        """Reload rule-engine state and temperature series from the database after a restart."""
        states = []
        for t in self.store.tags():
            states.append(TagState(box_id=t["box_id"], stage=t["stage"], last_seq=t["last_seq"],
                                   last_seen=t["last_seen"], last_sample=t["last_sample"] or 0,
                                   wr_count=t["wr_count"], lost=bool(t["lost"]), batt_low=bool(t["batt_low"]),
                                   pkts=t["pkts"], missed=t["missed"], first_seen=t["first_seen"],
                                   stage_since=t["stage_since"], sensor_type=t["sensor_type"] or 0,
                                   rssi=t["rssi"] or 0))
            self.product_by_tag.setdefault(t["box_id"], t["product"] or self.default_product)
            ser = self.store.series(t["box_id"], limit=200)
            temps, last = [], None
            for r in ser:
                if r["temp_c"] is None:
                    continue
                if last is None or r["ts"] - last >= SAMPLE_H * 3600 * 0.9:
                    temps.append(r["temp_c"]); last = r["ts"]
                else:
                    temps[-1] = max(temps[-1], r["temp_c"])
            self._series[t["box_id"]] = temps
            self._last_series_ts[t["box_id"]] = last
        self.rules.load(states)
        return len(states)

    def product(self, box_id):
        t = self.store.tag(box_id)                      # DB is the source of truth (shared with the bot)
        if t and t["product"]:
            return t["product"]
        return self.product_by_tag.get(box_id, self.default_product)

    def _series_from_db(self, box_id):
        temps, last = [], None
        for r in self.store.series(box_id, limit=400):
            if r["temp_c"] is None:
                continue
            if last is None or r["ts"] - last >= SAMPLE_H * 3600 * 0.9:
                temps.append(r["temp_c"]); last = r["ts"]
            else:
                temps[-1] = max(temps[-1], r["temp_c"])
        return temps

    def set_product(self, box_id, product, ts=None):
        """Change a box's product (changes its shelf-life model) and refresh its prediction now."""
        if product not in PRODUCTS:
            raise ValueError(f"unknown product {product!r}; choose from {sorted(PRODUCTS)}")
        t = self.store.tag(box_id)
        if not t:
            raise KeyError(box_id)
        self.store.set_product(box_id, product)
        self.product_by_tag[box_id] = product
        temps = self._series_from_db(box_id)
        self._series[box_id] = temps
        hours = self._store_prediction(box_id, ts or time.time(), product, t["stage"])
        return hours

    # ---------------- ingest ----------------
    def ingest_raw(self, raw: bytes, ts: float, rssi: int = 0, gateway: Optional[str] = None):
        try:
            p = parse_frame(raw, rssi)
        except FrameError as e:
            self.stats["raw_bad_crc"] += 1
            return {"accepted": False, "reason": str(e)}
        self.stats["raw_ok"] += 1
        return self.ingest_packet(p, ts, gateway)

    def ingest_host_record(self, rec: bytes, ts: float, gateway: Optional[str] = None):
        try:
            hr = parse_host_record(rec)
        except FrameError as e:
            return {"accepted": False, "reason": str(e)}
        self.stats["host_records"] += 1
        return self.ingest_packet(hr.packet, ts, gateway)

    def ingest_packet(self, p: Packet, ts: float, gateway: Optional[str] = None):
        out = self.rules.process(p, ts)
        if not out.accepted:
            self.stats["duplicates"] += 1
            return {"accepted": False, "reason": out.reason}
        temp = None if p.ack else decode_temp(p.sample, p.sensor_type)
        self.store.add_packet(ts, p, temp, out.kind, out.gap)
        self.store.save_tag(out.state, self.product(p.box_id))
        if gateway:
            self.store.set_location(p.box_id, gateway, ts)
        pred = None
        if temp is not None:
            pred = self._update_prediction(p.box_id, temp, ts)
        if temp is not None:
            out.alerts.extend(self._limit_alerts(p.box_id, self.product(p.box_id), temp, ts))
        for a in out.alerts:                 # alerts last: notifiers then see state + prediction + location
            self.store.add_alert(a)
        return {"accepted": True, "kind": out.kind, "gap": out.gap, "stage": STAGES[out.state.stage],
                "alerts": [a.kind for a in out.alerts], "remaining_h": pred}

    def _update_prediction(self, box_id, temp, ts):
        # Keep one sample per heartbeat slot so the model sees a ~6-hourly series even when
        # the tag is in alert mode and transmits every few minutes.
        last = self._last_series_ts.get(box_id)
        if last is None or ts - last >= SAMPLE_H * 3600 * 0.9 or box_id not in self._series:
            self._series.setdefault(box_id, []).append(temp)
            self._last_series_ts[box_id] = ts
        else:
            self._series[box_id][-1] = max(self._series[box_id][-1], temp)   # keep the worst in slot
        return self._store_prediction(box_id, ts, self.product(box_id), self.rules.tags[box_id].stage)

    def _store_prediction(self, box_id, ts, prod, stage):
        temps = self._series.get(box_id, [])
        if stage == 2:                                    # CRITICAL: no remaining life
            lo = mid = hi = 0.0
        else:
            lo, mid, hi = self.model.predict_range(temps, prod, stage)
        self.store.add_prediction(ts, box_id, mid, prod,
                                  "gbr" if getattr(self.model, "model", None) is not None else "physics", lo, hi)
        return mid

    def _limit_alerts(self, box_id, product, temp, ts):
        """Product-specific hard limits (WHO PQS for vaccines). Alerts once per violation episode."""
        out, active = [], self._limit_state.setdefault(box_id, set())
        if product == "vaccine":
            checks = {"FREEZE": temp <= VACCINE_FREEZE_C, "HEAT_LIMIT": temp > VACCINE_MAX_C}
            msgs = {"FREEZE": f"sample {temp:.1f} C <= {VACCINE_FREEZE_C} C: freeze risk (WHO PQS limit). The tag's heat "
                              "stage does NOT latch freezing - inspect/discard per SOP",
                    "HEAT_LIMIT": f"sample {temp:.1f} C > {VACCINE_MAX_C:.0f} C (WHO PQS: no excursion above +20 C)"}
            for k, bad in checks.items():
                if bad and k not in active:
                    active.add(k); out.append(Alert(ts, box_id, k, "critical", msgs[k]))
                elif not bad:
                    active.discard(k)
        return out

    def sweep(self, now: float):
        alerts = self.rules.sweep(now)
        for a in alerts:
            self.store.add_alert(a)
            self.store.save_tag(self.rules.tags[a.box_id], self.product(a.box_id))
        return alerts

    # ---------------- views ----------------
    def dispatch_ranking(self) -> List[dict]:
        """FEFO: smallest predicted remaining life first. CRITICAL / LOST units are pulled out
        of the dispatch list and shown as 'quarantine' / 'locate' rows on top."""
        preds = {r["box_id"]: r for r in self.store.latest_predictions()}
        rows = []
        for t in self.store.tags():
            pr = preds.get(t["box_id"])
            rem = pr["remaining_h"] if pr else None
            if t["stage"] == 2:
                action, prio = "QUARANTINE - inspect before dispatch", 0
            elif t["lost"]:
                action, prio = "LOCATE - tag silent", 1
            else:
                action, prio = "DISPATCH (FEFO)", 2
            rows.append({"box_id": f"{t['box_id']:08X}", "stage": STAGES[t["stage"]], "action": action,
                         "remaining_h": None if rem is None else round(rem, 1),
                         "remaining_days": None if rem is None else round(rem / 24, 1),
                         "product": t["product"], "lost": bool(t["lost"]), "_prio": prio,
                         "_rem": rem if rem is not None else 1e12})
        rows.sort(key=lambda r: (r["_prio"], r["_rem"]))
        for i, r in enumerate(rows, 1):
            r["rank"] = i
            del r["_prio"], r["_rem"]
        return rows
