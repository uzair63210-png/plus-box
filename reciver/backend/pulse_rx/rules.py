"""Deterministic risk rules (the 'Rule-Based Risk Checks' box). No I/O, fully unit-testable.

Mirrors RTL/tag_table.v but is the authoritative, persisted version:
  * duplicate drop (same box_id + seq)
  * latched stage never goes backwards (except after an authenticated restart)
  * CRITICAL -> immediate alert; AGING -> warning
  * ack packets: config-write ACK vs RESTART (write-counter changed)
  * seq gaps -> missed-packet statistics (collisions / range)
  * 2 missed 6-hourly heartbeats -> LOST;  seen again -> RECOVERED
  * batt_low flag -> maintenance alert
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from .protocol import Packet, STAGES

HB_PERIOD_S = 21600
HB_JITTER_S = 255
LOST_AFTER_S = 43800            # 2 x (6 h + jitter) - same constant as RTL LOST_TIMEOUT
ALERT_LOST_AFTER_S = 1800       # tag in AGING/CRITICAL repeats every <=300 s: 30 min silence = lost (RTL LOST_TIMEOUT_ALERT)


@dataclass
class TagState:
    box_id: int
    stage: int = 0
    last_seq: Optional[int] = None
    last_seen: float = 0.0
    last_sample: int = 0
    wr_count: Optional[int] = None
    lost: bool = False
    batt_low: bool = False
    pkts: int = 0
    missed: int = 0
    first_seen: float = 0.0
    stage_since: float = 0.0
    sensor_type: int = 0
    rssi: int = 0


@dataclass
class Alert:
    ts: float
    box_id: int
    kind: str            # CRITICAL | AGING | LOST | RECOVERED | RESTART | ANOMALY | BATT_LOW | NEW_TAG
    severity: str        # info | warning | critical
    detail: str


@dataclass
class Outcome:
    accepted: bool
    reason: str = ""
    kind: str = ""                    # heartbeat | alert_repeat | query | ack | restart | new | ...
    gap: int = 0
    alerts: List[Alert] = field(default_factory=list)
    state: Optional[TagState] = None


class RuleEngine:
    def __init__(self, lost_after_s: float = LOST_AFTER_S, alert_lost_after_s: float = ALERT_LOST_AFTER_S):
        self.lost_after_s = lost_after_s
        self.alert_lost_after_s = alert_lost_after_s
        self.tags: Dict[int, TagState] = {}

    def load(self, states):
        for s in states:
            self.tags[s.box_id] = s

    def process(self, p: Packet, ts: float) -> Outcome:
        out = Outcome(accepted=True)
        st = self.tags.get(p.box_id)
        if st is None:
            st = TagState(box_id=p.box_id, stage=p.stage, last_seq=p.seq, last_seen=ts,
                          last_sample=p.sample, first_seen=ts, stage_since=ts, pkts=1,
                          wr_count=p.sample if p.ack else 0, batt_low=p.batt_low,
                          sensor_type=p.sensor_type, rssi=p.rssi)
            self.tags[p.box_id] = st
            out.kind, out.state = "new", st
            out.alerts.append(Alert(ts, p.box_id, "NEW_TAG", "info", f"tag registered, stage {p.stage_name}"))
            self._stage_alerts(p, st, ts, out, entering=p.stage > 0)
            return out

        out.state = st
        if p.seq == st.last_seq:
            return Outcome(accepted=False, reason="duplicate", state=st)

        gap = (p.seq - st.last_seq - 1) & 0xFF
        out.gap = gap
        st.missed += gap
        st.last_seq = p.seq
        st.pkts += 1
        was_lost = st.lost
        st.lost = False
        st.last_seen = ts
        st.rssi = p.rssi
        st.sensor_type = p.sensor_type

        if p.batt_low and not st.batt_low:
            out.alerts.append(Alert(ts, p.box_id, "BATT_LOW", "warning", "coin cell low - replace tag"))
        st.batt_low = p.batt_low

        if p.ack:
            if p.sample != st.wr_count:
                st.wr_count = p.sample
                st.stage, st.stage_since = p.stage, ts
                out.kind = "restart"
                out.alerts.append(Alert(ts, p.box_id, "RESTART", "info",
                                        f"authenticated restart #{p.sample}; stage reset to {p.stage_name}"))
            else:
                out.kind = "ack"
        else:
            st.last_sample = p.sample
            if p.stage < st.stage:
                out.kind = "anomaly"
                out.alerts.append(Alert(ts, p.box_id, "ANOMALY", "critical",
                                        f"stage regressed {STAGES[st.stage]}->{p.stage_name} without restart "
                                        "(possible brown-out or tampering)"))
            elif p.stage > st.stage:
                out.kind = "stage_up"
                self._stage_alerts(p, st, ts, out, entering=True, prev=st.stage)
                st.stage, st.stage_since = p.stage, ts
            else:
                out.kind = "query" if p.query else ("alert_repeat" if p.stage else "heartbeat")
        if was_lost:
            out.alerts.append(Alert(ts, p.box_id, "RECOVERED", "info", "tag heard again after being lost"))
        return out

    @staticmethod
    def _stage_alerts(p, st, ts, out, entering, prev=None):
        if not entering:
            return
        frm = "NEW" if prev is None else STAGES[prev]
        if p.stage == 2:
            out.alerts.append(Alert(ts, p.box_id, "CRITICAL", "critical",
                                    f"{frm} -> CRITICAL: excursion/expiry latched, quarantine unit"))
        elif p.stage == 1:
            out.alerts.append(Alert(ts, p.box_id, "AGING", "warning",
                                    f"{frm} -> AGING: shelf life entering AGING stage"))

    def sweep(self, now: float) -> List[Alert]:
        """Flag silent tags: 2 missed heartbeats (FRESH) or ~6 missed repeat packets (AGING/CRITICAL)."""
        alerts = []
        for st in self.tags.values():
            limit = self.alert_lost_after_s if st.stage > 0 else self.lost_after_s
            if not st.lost and now - st.last_seen >= limit:
                st.lost = True
                silent = now - st.last_seen
                txt = f"{int(silent // 60)} min" if silent < 7200 else f"{int(silent // 3600)} h"
                why = "tag was in alert mode (repeats every <=5 min)" if st.stage > 0 else "2 missed heartbeats"
                alerts.append(Alert(now, st.box_id, "LOST", "critical" if st.stage else "warning",
                                    f"no packet for {txt} ({why}); last stage {STAGES[st.stage]}"))
        return alerts
