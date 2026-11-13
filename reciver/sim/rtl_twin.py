"""Cycle-level Python twin of RTL/rx_bit_sync.v + RTL/pkt_rx.v (same registers, same update order).
Purpose: check the *algorithm* (async clock, jitter, noise, sync tolerance, CRC) on machines without
a Verilog simulator. It does NOT prove the Verilog compiles - run testbench/tb_pulse_rx.v for that.
"""
import os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from pulse_rx.protocol import Packet, build_frame, parse_frame


class BitSync:
    def __init__(self, cpb):
        self.half, self.last = cpb // 2, cpb - 1
        self.s1 = self.s2 = self.s3 = 0
        self.phase = 0
        self.valid = 0; self.data = 0

    def clock(self, rx):
        edge = self.s2 ^ self.s3
        n_phase = 0 if edge else (0 if self.phase == self.last else self.phase + 1)
        n_valid, n_data = 0, self.data
        if (not edge) and self.phase == self.half:
            n_valid, n_data = 1, self.s2
        self.s1, self.s2, self.s3 = rx, self.s1, self.s2
        self.phase, self.valid, self.data = n_phase, n_valid, n_data


class PktRx:
    PATTERN, TOL = 0xAA2DD4, 1

    def __init__(self):
        self.state = 0; self.sh = 0; self.cnt = 0; self.crc = 0xFFFF; self.pay = 0
        self.sync_hit = self.pkt_valid = self.crc_err = 0
        self.fields = None

    def clock(self, bv, bd):
        self.sync_hit = self.pkt_valid = self.crc_err = 0
        if not bv:
            return
        sh_n = ((self.sh << 1) | bd) & 0xFFFFFF
        sync_ok = bin((sh_n ^ self.PATTERN) & 0xFFFFF).count("1") <= self.TOL
        if self.state == 0:
            self.sh = sh_n
            if sync_ok:
                self.state, self.cnt, self.crc, self.sync_hit = 1, 0, 0xFFFF, 1
        else:
            self.sh = sh_n
            if sh_n == self.PATTERN:                     # exact hit inside a (false) body: restart
                self.cnt, self.crc, self.sync_hit = 0, 0xFFFF, 1
                return
            fb = ((self.crc >> 15) & 1) ^ bd
            crc_n = (((self.crc << 1) & 0xFFFF) ^ 0x1021) if fb else (self.crc << 1) & 0xFFFF
            self.crc = crc_n
            pay_n = ((self.pay << 1) | bd) & ((1 << 56) - 1)
            if self.cnt < 56:
                self.pay = pay_n
            if self.cnt == 71:
                self.state, self.sh = 0, 0
                if crc_n == 0:
                    self.pkt_valid = 1
                    p = self.pay
                    self.fields = ((p >> 24) & 0xFFFFFFFF, (p >> 16) & 0xFF, (p >> 8) & 0xFF, p & 0xFF)
                else:
                    self.crc_err = 1
            else:
                self.cnt += 1


def bits_of(frame: bytes):
    return [(b >> (7 - i)) & 1 for b in frame for i in range(8)]


def run(frames, cpb=16, ppm=3000, flips=None, noise_gap_bits=40, seed=0, ber_during_idle=0.5):
    """frames: list of 12-byte frames. flips: {frame_index: [bit positions to invert]}.
    Between frames the channel carries random noise (FSK demod of nothing) for noise_gap_bits."""
    rng = random.Random(seed)
    flips = flips or {}
    stream = []                                    # one entry per TAG bit
    marks = []
    for i, fr in enumerate(frames):
        stream += [int(rng.random() < ber_during_idle) for _ in range(noise_gap_bits + rng.randint(0, 9))]
        b = bits_of(fr)
        for pos in flips.get(i, []):
            b[pos] ^= 1
        marks.append(len(stream)); stream += b
    stream += [0] * 40
    # resample at receiver clock: tag bit duration = cpb*(1+ppm*1e-6) rx clocks, random start phase
    bit_clks = cpb * (1 + ppm * 1e-6)
    bs, pk = BitSync(cpb), PktRx()
    out = {"ok": [], "crc_err": 0, "sync": 0}
    t = rng.uniform(0, cpb)
    total = int(len(stream) * bit_clks + 2 * cpb)
    for clk in range(total):
        idx = int((clk + t) / bit_clks)
        rx = stream[idx] if idx < len(stream) else 0
        pk.clock(bs.valid, bs.data)                # pkt_rx sees last cycle's registered outputs
        bs.clock(rx)
        if pk.sync_hit: out["sync"] += 1
        if pk.pkt_valid: out["ok"].append(pk.fields)
        if pk.crc_err: out["crc_err"] += 1
    return out


def expected(frames):
    res = []
    for f in frames:
        p = parse_frame(f)
        res.append((p.box_id, p.status_byte, p.sample, p.seq))
    return res


if __name__ == "__main__":
    rng = random.Random(5)
    frames = [build_frame(Packet(rng.getrandbits(32) if i % 3 else 0xB0A00001, rng.randint(0, 2), rng.randint(0, 7),
                                 bool(rng.getrandbits(1)), False, False, rng.randint(0, 255), i & 0xFF))
              for i in range(200)]
    exp = expected(frames)
    fails = 0
    # tag runs from a 32.768 kHz crystal (~+-100 ppm); sweep far beyond that. 16x oversampling is
    # verified to +-1.5 %, 64x to +-2 % (the -2 % / 16x corner is marginal on frames with long runs).
    for cpb, offsets in ((16, (0, 3000, -3000, 15000, -15000)), (64, (0, 3000, -3000, 20000, -20000))):
        for ppm in offsets:
            r = run(frames, cpb=cpb, ppm=ppm, seed=cpb + ppm)
            good = r["ok"] == exp and r["crc_err"] == 0
            print(f"clean   cpb={cpb:3d} clock-offset={ppm/1e4:+5.1f}%  decoded {len(r['ok'])}/200 "
                  f"crc_err={r['crc_err']}  {'PASS' if good else 'FAIL'}")
            fails += not good
    # corruption: 1 flipped bit inside payload/CRC -> must be rejected, never decoded as a different packet
    flips = {i: [rng.randint(24, 95)] for i in range(0, 200, 4)}
    r = run(frames, cpb=16, ppm=3000, flips=flips, seed=11)
    wrong = [f for f in r["ok"] if f not in exp]
    ok_expected = [e for i, e in enumerate(exp) if i not in flips]
    good = (not wrong) and r["ok"] == ok_expected and r["crc_err"] == len(flips)
    print(f"1-bit error in 50 frames: accepted={len(r['ok'])} rejected_by_crc={r['crc_err']} "
          f"wrong_accepts={len(wrong)}  {'PASS' if good else 'FAIL'}")
    fails += not good
    # sync-word error tolerance (1 bad bit in sync still decodes; 3 bad bits does not start a frame)
    r1 = run(frames[:40], cpb=16, flips={i: [8 + 5] for i in range(40)}, seed=3)
    r3 = run(frames[:40], cpb=16, flips={i: [9, 14, 20] for i in range(40)}, seed=3)
    good = r1["ok"] == exp[:40] and len(r3["ok"]) == 0
    print(f"sync tolerance: 1 bad sync bit -> {len(r1['ok'])}/40 decoded, 3 bad sync bits -> {len(r3['ok'])}/40  "
          f"{'PASS' if good else 'FAIL'}")
    fails += not good
    # pure noise: no frame may pass CRC
    noise = [bytes(rng.getrandbits(8) for _ in range(12)) for _ in range(2000)]
    r = run(noise, cpb=16, seed=2, noise_gap_bits=0)
    print(f"noise-only: {len(r['ok'])} false packets passed CRC out of 2000 random frames (sync hits {r['sync']})")
    sys.exit(1 if fails else 0)
