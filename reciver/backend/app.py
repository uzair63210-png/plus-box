"""FastAPI backend.   python app.py        (or: uvicorn app:app --port 8000)
Needs the pulse_rx/ folder next to this file.
env: DATABASE_URL (default sqlite:///pulse_rx.db = local file; postgresql://user:pw@host/db for production)
"""
import os
import sys
import threading
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # find ./pulse_rx wherever you run from
os.chdir(os.path.dirname(os.path.abspath(__file__)))             # keep pulse_rx.db next to the app

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from pulse_rx.db import Store
from pulse_rx.security import key_ok
from pulse_rx.service import Pipeline

pipeline = Pipeline(Store())
pipeline.restore()
app = FastAPI(title="PULSE-Box receiver backend", version="1.1")
API_KEY = os.environ.get("PULSE_API_KEY")


def auth(x_api_key: Optional[str] = Header(default=None)):
    """Every endpoint except /health needs header  X-API-Key: <PULSE_API_KEY>  when the variable is set."""
    if not key_ok(x_api_key, API_KEY):
        raise HTTPException(401, "invalid or missing X-API-Key")



class RawIn(BaseModel):
    frame_hex: str              # 12-byte over-the-air frame, e.g. from an SX127x/CC1101 gateway
    rssi: int = 0
    ts: Optional[float] = None
    gateway: Optional[str] = None     # receiver id, e.g. "cold-room-1" (looked up in gateways.json)


class HostIn(BaseModel):
    record_hex: str             # 12-byte record from the FPGA gateway UART (RTL/host_if.v)
    ts: Optional[float] = None
    gateway: Optional[str] = None


def _sweeper():
    while True:
        time.sleep(60)
        pipeline.sweep(time.time())


@app.on_event("startup")
def _start():
    if not API_KEY:
        print("WARNING: PULSE_API_KEY is not set - the API is open to anyone who can reach this port.")
    threading.Thread(target=_sweeper, daemon=True).start()
    if os.environ.get("TELEGRAM_BOT_TOKEN"):             # Telegram alerts run inside the same process
        from pulse_rx.telegram_bot import Notifier
        bot = Notifier(pipeline)
        bot.init_cursor()
        bot.start_background()
        print("Telegram bot started")


@app.post("/ingest/raw", dependencies=[Depends(auth)])
def ingest_raw(body: RawIn):
    try:
        raw = bytes.fromhex(body.frame_hex)
    except ValueError:
        raise HTTPException(400, "frame_hex is not valid hex")
    return pipeline.ingest_raw(raw, body.ts or time.time(), body.rssi, body.gateway)


@app.post("/ingest/host", dependencies=[Depends(auth)])
def ingest_host(body: HostIn):
    try:
        rec = bytes.fromhex(body.record_hex)
    except ValueError:
        raise HTTPException(400, "record_hex is not valid hex")
    return pipeline.ingest_host_record(rec, body.ts or time.time(), body.gateway)


@app.get("/tags", dependencies=[Depends(auth)])
def tags():
    return pipeline.store.tags()


@app.get("/alerts", dependencies=[Depends(auth)])
def alerts(limit: int = 200):
    return pipeline.store.alerts(limit)


@app.get("/dispatch", dependencies=[Depends(auth)])
def dispatch():
    return pipeline.dispatch_ranking()


@app.get("/series/{box_id}", dependencies=[Depends(auth)])
def series(box_id: int, limit: int = 500):
    return pipeline.store.series(box_id, limit)


@app.get("/stats", dependencies=[Depends(auth)])
def stats():
    return pipeline.stats


@app.get("/health")
def health():
    return {"ok": True, "tags": len(pipeline.rules.tags)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)      # docs at http://127.0.0.1:8000/docs
