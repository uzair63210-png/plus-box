# PULSE-Box (plus-box)

Reusable coin-cell shelf-life monitoring tag for cold-chain logistics, plus a matching receiver.
A tag silently integrates **time × temperature** on-chip, latches a tamper-evident **FRESH → AGING → CRITICAL**
stage (it can never go backwards), and reports it over a 433.92 MHz sub-GHz FSK uplink. Configuration is written
through a **write-only 125 kHz inductive link** — the tag can never read its configuration back, so its spoilage
record can't be silently cleared. An operator can also query status on the shelf with **3 flashes of a UV-A torch**.

&gt; For the full receiver-side design, verification results, Telegram bot docs and honest status notes, see [`reciver/README.md`](reciver/README.md).

## Why

- A printed expiry date assumes a perfect cold chain. One heat excursion in a delayed truck can spoil dairy,
  vaccines or packaged food days early — with no visible sign on the carton.
- Passive time-temperature labels are single-use and visual-only.
- Electronic data loggers cost ₹4,000+ each and stay silent until someone interrogates them — silence means
  "fresh" or "dead tag", nobody can tell.
- Any resettable tag can have its spoilage flag cleared by an unauthorized person.
- Hundreds of tags transmitting at fixed intervals in one cold room collide and waste battery.

PULSE-Box puts a ~₹200–430 (volume) tag on each crate/pallet that answers all five problems: always-on latching
stage, sub-second alert on excursion, 6-hourly heartbeat that exposes silent tag death, per-box jitter against
collisions, and a write-only config link that closes the tamper loop.

## System overview

```
 Write-link programmer          PULSE-Box TAG (coin cell, single clock)          Receiver / gateway
 125 kHz coil + frames   ──►   wlink_rx → wlink_regfile (write-only, pw+CRC,   rx_bit_sync → pkt_rx → tag_table
                               5 s lockout, write-counter)                            (CDR)       (CRC, dedupe, latch)
        │                      stage_engine (age + Σtime×temp, latch FSM)              │
        │                      tx_scheduler (6 h heartbeat, alert 10→300 s, LFSR jitter) └─► host_if (UART) ──► backend
        ▼                      packet_tx → 433.92 MHz FSK (±36 kHz, 4.096 kbps)               FastAPI + rules + shelf-life ML
 UV-A torch (365 nm)    ──►   uv_query (3 pulses/2 s window, 60 s lockout)                   + PostgreSQL/SQLite + Streamlit
                               + Telegram alert bot (stage change / lost / restart)
```

- **Uplink packet (72-bit body):** preamble `AA` · sync `2DD4` · Box ID 32b · status 8b (stage/ack/UV/batt) ·
  sample 8b · seq 8b · CRC-16/CCITT-FALSE. NRZ at 4.096 kbps → ~23 ms on air.
- **Config frame (write-only, 125 kHz):** 92-bit pulse-interval-coded frame, header + register address + 32-bit
  data + 32-bit password, CRC-16. Rising-edge intervals ~500 µs (0) / ~1000 µs (1) tolerate ±25 % oscillator
  error. Wrong password → 5 s lock-out. No read path exists.
- **Optical query:** 3 UV-A flashes within 2 s = instant status packet; 60 s lockout after a query;
  hysteresis + high-pass reject sunlight and lamp flicker.

## Repository layout

| Path | What it is |
|:---|:---|
| `RTL/` | Tag digital core, Verilog-2001, single 32.768 kHz clock domain: `tick_gen` (32.768 kHz→1 Hz), `wlink_rx`, `wlink_regfile` (password, lock-out, write-counter, authenticated restart), `stage_engine` (age counter + time×temp accumulator + latched FSM), `uv_query`, `tx_scheduler` (heartbeat, alert back-off, LFSR jitter), `packet_tx`, `pulse_tag_core` top. Note: `tick gen.v` has a space in the filename. |
| `testbench/tb_plus_boc.v` | Tag testbench — wrong-password / bad-CRC / lock-out frames dropped, authenticated restart, heat excursion to CRITICAL, UV query codes, ack packets. |
| `ngspice/` | Analog front-end netlists: `fsk_tx.cir` (common-base Colpitts, 433.92/433.85 MHz, −5.7 dBm, 2.6 mA), `wl_detector.cir` (LC tank + Schottky envelope + comparator), `uv_frontend.cir` (photodiode + TIA + high-pass + hysteresis comparator). Plus run logs. |
| `reciver/` | **Receiver side** — see its own README: `rtl/` (bit sync/CDR, packet RX, tag table with dedupe + lost-tag detection, UART host interface), `sim/rtl_twin.py` (cycle-level Python twin), `backend/` (FastAPI, rules, Q10+ML shelf-life model, PostgreSQL/SQLite, Streamlit dashboard, virtual-tag simulator), Telegram alert bot. |
| `PULSE-Box_Schematic_Rev-A.pdf` | Complete tag schematic (Rev A). |
| `PULSE-Box_Round_3D_model.obj` | Round IP65 housing 3D model. |
| `telegram_bot.py`, `run_bot.py`, `gateways.json` | Convenience copies of the Telegram bot at repo root (canonical copies live in `reciver/backend/`). |

## Running things

Tag RTL (Icarus Verilog):

```bash
iverilog -g2005 -o tag_sim RTL/*.v testbench/tb_plus_boc.v && vvp tag_sim
```

Analog blocks (ngspice):

```bash
ngspice ngspice/fsk_tx.cir        # FSK transmitter
ngspice ngspice/wl_detector.cir   # write-link detector
ngspice ngspice/uv_frontend.cir   # UV query front-end
```

Receiver backend (details and honest verification status in `reciver/README.md`):

```bash
pip install -r reciver/backend/requirements.txt
cd reciver/backend
python tests/test_core.py     # 13 tests
python bridge.py --demo --db sqlite:///pulse_rx.db
streamlit run dashboard.py
```

## Design targets

| Parameter | Target |
|:---|:---|
| Sleep current | ≤ 1–2 µA (only counter/comparator/regfile always on) |
| Battery life | ≥ 2 years on CR1220 (~35 mAh); TX draws 2.6 mA only during ~23 ms packets |
| Reporting | 6 h heartbeat + jitter; alert repeats with back-off 10 s → 300 s |
| Security | 32-bit password per config frame, 5 s fail lock-out, latched stage, write-counter in every ack |
| Radio | 433.92 MHz FSK ±36 kHz, 4.096 kbps NRZ (WPC licence-exempt band, ≤ 10 mW e.r.p., ≤ 10 % duty cycle) |
| Lost-tag detection | 2 missed 6 h heartbeats → "lost" flag at receiver |

## Honest status

- Tag digital core: simulated and passing testbenches (Icarus Verilog) — wrong-password, bad-CRC and lock-out
  frames dropped with zero side effects, authenticated restart, excursion-to-CRITICAL latching, UV codes, acks.
- Analog blocks: verified in ngspice/LTspice simulation only — bench validation pending.
- Receiver RTL: validated via a cycle-level Python twin (200/200 frames at 0/±0.3 % clock offset, single-bit
  errors rejected, no false packets in 2000 random frames); the twin caught and drove a real false-lock bug fix.
- Shelf-life ML: trained on synthetic Q10 data — physics-only error cut ~15 % by the learned correction on
  synthetic held-out profiles; says nothing about real products yet. Re-train with real data (`ShelfLifeModel.fit`).
- Known gaps: no non-volatile storage yet (brown-out loses age/stage — FRAM/EEPROM checkpoint planned);
  frequency accuracy of a free-running oscillator needs a SAW resonator or a crystal-based SoC fallback;
  `app.py`/`dashboard.py` are compile-checked, the logic underneath them is tested.

## License / attribution

Hardware design and RTL by Uzair Sajid Shaikh. If you build on this, keep the write-only-link and latching-stage
sections intact — they are the parts that make the spoilage record trustworthy.
