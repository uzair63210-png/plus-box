"""Gateway bridge: FPGA receiver UART (12-byte records) -> backend.
  python bridge.py --serial /dev/ttyUSB0 --api http://localhost:8000
  python bridge.py --serial COM5 --db sqlite:///pulse_rx.db        (write straight to the database)
  python bridge.py --demo --db sqlite:///pulse_rx.db               (virtual fleet, no hardware)
"""
import argparse
import json
import os
import time
import urllib.request

from pulse_rx.protocol import parse_host_record, FrameError


def records(stream_read):
    """Yield validated 12-byte records, resynchronising on 0x7E + XOR checksum."""
    buf = bytearray()
    while True:
        chunk = stream_read()
        if chunk:
            buf += chunk
        while len(buf) >= 12:
            if buf[0] != 0x7E:
                del buf[0]
                continue
            try:
                parse_host_record(bytes(buf[:12]))
            except FrameError:
                del buf[0]                      # 0x7E inside a payload or corruption: slide one byte
                continue
            yield bytes(buf[:12])
            del buf[:12]
        if not chunk:
            time.sleep(0.01)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial"); ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--api"); ap.add_argument("--db")
    ap.add_argument("--gateway", help="receiver id from gateways.json, e.g. cold-room-1")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()

    if a.demo:
        from pulse_rx.db import Store
        from pulse_rx.service import Pipeline
        from pulse_rx.simulator import SimTag, cold_chain, with_excursion, run
        pl = Pipeline(Store(a.db or "sqlite:///pulse_rx.db"))
        tags = [SimTag(0xB0A00001 + i, cold_chain(3 + i * 0.7)) for i in range(6)]
        tags[2].profile = with_excursion(cold_chain(), 20 * 3600, 8 * 3600, 42)
        tags[4].die_at = 30 * 3600
        print(run(pl, tags, 4 * 86400, gateway="cold-room-1"))
        return

    import serial                                        # pip install pyserial
    port = serial.Serial(a.serial, a.baud, timeout=0.05)
    pipeline = None
    if a.db:
        from pulse_rx.db import Store
        from pulse_rx.service import Pipeline
        pipeline = Pipeline(Store(a.db)); pipeline.restore()
    for rec in records(lambda: port.read(64)):
        if pipeline:
            print(pipeline.ingest_host_record(rec, time.time(), a.gateway))
        else:
            req = urllib.request.Request(a.api.rstrip("/") + "/ingest/host",
                                         data=json.dumps({"record_hex": rec.hex(), "gateway": a.gateway}).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "X-API-Key": os.environ.get("PULSE_API_KEY", "")})
            print(urllib.request.urlopen(req, timeout=5).read().decode())


if __name__ == "__main__":
    main()
