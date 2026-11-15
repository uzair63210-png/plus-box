# PULSE-Box receiver side - RTL + system

Receiver for the PULSE-Box tag (`plus-box` repo). Matches the tag's packet format bit-for-bit
(`RTL/packet_tx.v`): `AA | 2DD4 | ID32 | status8 | sample8 | seq8 | CRC16`, CRC-16/CCITT-FALSE, 4.096 kbps NRZ.

```
 433.92 MHz FSK demod (sliced)            FPGA / ASIC gateway (RTL/)                       Host (backend/)
 rx_data ──► rx_bit_sync ──► pkt_rx ──► tag_table ──► host_if (FIFO + UART) ──► bridge.py ──► FastAPI
              (CDR)        (sync+CRC)   (dedupe, stage,      12-byte records                    ├─ rules.py   (risk checks)
                                         lost-tag, restart)                                      ├─ shelf_life.py (ML, FEFO)
                                                                                                 ├─ PostgreSQL (schema.sql)
                                                                                                 └─ dashboard.py (Streamlit)
```

## RTL (`RTL/`, Verilog-2001, single clock domain)
| file | job |
|---|---|
| `rx_bit_sync.v` | digital clock/data recovery: re-aligns on every edge, samples mid-bit, free-runs through long runs |
| `pkt_rx.v` | hunts last-4-preamble-bits + sync word (1 bit error allowed), 72-bit body, CRC check by running CRC over payload+CRC (=0). Exact 24-bit match inside a body restarts the frame (false-lock protection) |
| `tag_table.v` | N-entry tag table: drops duplicates (id+seq), latches stage (never backwards), separates config ACK from authenticated RESTART via write-counter, seq-gap count, LOST after `LOST_TIMEOUT` s (2 x 6 h heartbeat), RECOVERED |
| `uart_tx.v`, `host_if.v` | 8-deep event FIFO, 12-byte UART record `7E evt id[4] status sample seq gap rssi xor` |
| `pulse_rx_top.v` | integration; counters for ok / CRC-bad / duplicates / tags / lost |

Defaults assume a 6.5536 MHz clock (1600 clks/bit, 115200 baud = 57 clks). Event codes: see `backend/pulse_rx/protocol.py`.
The radio itself (a sub-GHz FSK receiver module/IC in raw mode that outputs `rx_data`, optional carrier-detect and RSSI) is outside the RTL.

## Backend (`backend/`)
`protocol.py` (frame/record codec) - `rules.py` (deterministic checks) - `shelf_life.py` (physics Q10 + learned residual) -
`service.py` (pipeline, FEFO ranking) - `db.py` (SQLite dev / PostgreSQL prod) - `app.py` (FastAPI) - `bridge.py` (UART->API) -
`dashboard.py` (Streamlit) - `simulator.py` (virtual tag fleet) - `gh_sync.py` (snapshot DB to a private GitHub repo).

Windows quick start: keep `app.py` and the `pulse_rx/` folder together, `pip install numpy scikit-learn fastapi uvicorn streamlit pandas`, then `python app.py` (local SQLite file, no server needed).

```bash
pip install -r backend/requirements.txt
cd backend
python tests/test_core.py                       # 15 tests
python tests/test_telegram.py                   # bot tests (fake Telegram server)
python tests/test_research_features.py          # MKT, WHO limits, intervals, AI assistant (fake Anthropic server)
python bridge.py --demo --db sqlite:///pulse_rx.db
streamlit run dashboard.py                      # or: uvicorn app:app ; python bridge.py --serial COM5 --api http://localhost:8000
```

## Telegram alert bot (`backend/pulse_rx/telegram_bot.py`, `run_bot.py`, `gateways.json`)
Pushes a message + map pin on every stage change (AGING / CRITICAL) and on LOST, RECOVERED, ANOMALY, BATT_LOW, RESTART:
box id, product, stage + time in stage, temperature, predicted remaining shelf life (and date), last heard, battery/RSSI,
packets missed, restart count, receiver location (name, Google-Maps link, Telegram location pin) and the recommended action.
**Research basis and what changed: see `RESEARCH.md`.**
AI assistant: `/ask <question>` (Claude, read-only tools; set `ANTHROPIC_API_KEY`, optional `PULSE_LLM_MODEL`, `PULSE_LLM_PER_HOUR`). Vaccine boxes get WHO-limit FREEZE / HEAT_LIMIT alerts - set the tag's `sensor_type` to 2 (0.5 °C steps from -40 °C) so sub-zero temperatures are visible.
Commands: `/start <passcode>`, `/stop`, `/status`, `/box <id>`, `/where <id>`, `/product <id> <dairy|frozen|vaccine|packaged>`, `/dispatch`, `/alerts`, `/help`.
* **Acknowledge + escalation:** CRITICAL / LOST / ANOMALY alerts have a ✅ Acknowledge button. Unacknowledged after `PULSE_ESCALATE_MIN` (15) minutes -> re-sent as ESCALATION, up to `PULSE_ESCALATE_MAX` (3) times. Anyone subscribed can acknowledge; everyone is told who did.
* **Burst grouping:** `PULSE_GROUP_MIN` (4) or more same-kind alerts in one batch (e.g. a truck going CRITICAL) become one message with one map pin per receiver.
* **Daily digest:** set `PULSE_DIGEST_HOUR` (local hour, e.g. 8).
* **Per-box product:** remaining shelf life depends on the product; `/product` changes it and recalculates immediately (stored in the DB).
* **API key:** set `PULSE_API_KEY`; every API endpoint except `/health` then needs header `X-API-Key`. `bridge.py` sends it automatically from the same variable.
* **Faster lost-tag rule:** tags in AGING/CRITICAL repeat every <=5 min, so 30 min of silence flags LOST (FRESH tags keep the 2-heartbeat rule). Mirrored in `tag_table.v` (`LOST_TIMEOUT_ALERT`).
Tags have no GPS: "location" = the receiver that last heard the box. Describe receivers in `gateways.json` and pass the
receiver id on ingest (`bridge.py --gateway cold-room-1`, or `"gateway"` in the API body).
Setup (PowerShell): create a bot with @BotFather, then
`$env:TELEGRAM_BOT_TOKEN="..."; $env:PULSE_BOT_PASSCODE="your-secret"; python run_bot.py` (or set the same variables before `python app.py`
and the bot runs inside the API process). In Telegram send `/start your-secret`. `python run_bot.py --test <chat_id>` sends a sample alert.
Tested against a fake Telegram server (`tests/test_telegram.py`); not yet against the real Telegram API.

## Verification status (be precise about what has and has not been run)
Run and passing (in the authoring environment):
* `python backend/tests/test_core.py` - 15/15, `test_telegram.py` 12/12 (fake Telegram server), `test_research_features.py` 10/10 (fake Anthropic server): CRC check value 0x29B1, bit-exact match to the tag's CRC function, all single-bit errors rejected, rules, restart/ack logic, lost/recovered, DB restore, UART resync, end-to-end 4-tag fleet (excursion -> CRITICAL, dead tag -> LOST, FEFO order).
* `python sim/rtl_twin.py` - cycle-level Python twin of `rx_bit_sync` + `pkt_rx` on an asynchronous clock: 200/200 frames decoded at 0 / +-0.3 % clock offset (16x) and up to +-1.5 % (16x) / +-2 % (64x); 50/50 single-bit errors rejected with 0 wrong accepts; 0 false packets from 2000 random frames. This twin found and drove the fix for a real false-lock bug (1-3 % frame loss in noise) in the first RTL draft.

NOT run here (no Verilog simulator / network in the authoring sandbox):
* The Verilog itself has not been compiled or simulated. `tag_table.v`, `host_if.v`, `uart_tx.v` and `pulse_rx_top.v` have no twin either. Run `testbench/tb_pulse_rx.v` (instantiates your real `pulse_tag_core`):
  `iverilog -g2005 -o rx_sim RTL/*.v plus-box-main/RTL/*.v testbench/tb_pulse_rx.v && vvp rx_sim` (quote `"tick gen.v"`, it has a space). Expect to fix small syntax/timing issues on first run.
* `app.py` (FastAPI) and `dashboard.py` (Streamlit) were compile-checked only; the logic they call is tested.

## Known limits / honest notes
* ML: trained on **synthetic** Q10 data. On held-out synthetic profiles the learned correction cuts error ~15 % vs physics-only; this says nothing about real products. Re-train with real data (`ShelfLifeModel.fit`). Temperature code -> deg C is assumed 1:1 (`decode_temp`).
* The tag's frame is 8-bit preamble + 7 payload bytes (incl. seq) + CRC. Slide 6 / Fig 2a shows a 16-bit preamble and no seq field - the RTL is the source of truth; fix the slide.
* Receiver never transmits; it cannot ack/retry tag packets (tag is TX-only except write-link).
