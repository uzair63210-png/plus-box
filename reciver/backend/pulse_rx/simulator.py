"""Virtual fleet of PULSE-Box tags (behavioural copy of stage_engine + tx_scheduler) that emits
bit-exact over-the-air frames. Used for demos, dashboard development and tests."""
import random
from .protocol import Packet, build_frame


class SimTag:
    def __init__(self, box_id, profile, expiry_s=14 * 86400, aging_win_s=2 * 86400, base=8,
                 acc_mild=3000, acc_severe=9000, sensor_type=1, die_at=None, seed=0):
        self.id, self.profile = box_id, profile
        self.expiry, self.win, self.base = expiry_s, aging_win_s, base
        self.mild, self.severe = acc_mild, acc_severe
        self.sensor_type, self.die_at = sensor_type, die_at
        self.rng = random.Random(seed ^ box_id)
        self.age = self.acc = 0
        self.stage = 0
        self.seq = 0
        self.timer = 2 + self.rng.randint(0, 255)
        self.alert_int = 10
        self.restart_n = 0

    def temp(self, t):
        return max(0, min(255, int(round(self.profile(t)))))

    def step(self, t):
        """Advance 1 second; return a frame (bytes) if the tag transmits now."""
        if self.die_at is not None and t >= self.die_at:
            return None
        self.age += 1
        if self.age % 60 == 0:
            self.acc += max(0, self.temp(t) - self.base)
        new = 2 if (self.age >= self.expiry or self.acc >= self.severe) else \
              1 if (self.age + self.win >= self.expiry or self.acc >= self.mild) else 0
        if new > self.stage:
            self.stage = new
            self.alert_int = 10
            self.timer = 1 + self.rng.randint(0, 7)
        if self.timer == 0:
            if self.stage == 0:
                self.timer = 21600 + self.rng.randint(0, 255)
            else:
                self.timer = self.alert_int + self.rng.randint(0, 7)
                self.alert_int = min(300, self.alert_int * 2)
            return self._frame()
        self.timer -= 1
        return None

    def _frame(self, ack=False, query=False, sample=None):
        p = Packet(self.id, self.stage, self.sensor_type, False, query, ack,
                   self.temp(0) if sample is None else sample, self.seq)
        self.seq = (self.seq + 1) & 0xFF
        return build_frame(p)


def cold_chain(base=4.0, daily=1.0):
    import math
    return lambda t: base + daily * math.sin(2 * math.pi * t / 86400)


def with_excursion(profile, start_s, dur_s, peak):
    def f(t):
        v = profile(t)
        if start_s <= t < start_s + dur_s:
            return peak
        return v
    return f


def run(pipeline, tags, duration_s, loss=0.01, seed=1, t0=1_800_000_000.0, collisions=True, gateway=None):
    """Drive the pipeline with all tags for duration_s virtual seconds. Returns (sent, delivered)."""
    rng = random.Random(seed)
    sent = delivered = 0
    for t in range(duration_s):
        slot_frames = []
        for tag in tags:
            fr = tag.step(t)
            if fr is not None:
                sent += 1
                slot_frames.append(fr)
        if collisions and len(slot_frames) > 1:       # >1 packet in the same second: ~23 ms each -> rare overlap
            slot_frames = [f for f in slot_frames if rng.random() > 0.05 * (len(slot_frames) - 1)]
        for fr in slot_frames:
            if rng.random() < loss:
                continue
            delivered += 1
            pipeline.ingest_raw(fr, t0 + t, rssi=rng.randint(60, 110), gateway=gateway)
        if t % 600 == 0:
            pipeline.sweep(t0 + t)
    pipeline.sweep(t0 + duration_s)
    return sent, delivered
