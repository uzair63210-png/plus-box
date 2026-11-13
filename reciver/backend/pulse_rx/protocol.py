"""Bit-exact PULSE-Box uplink protocol (matches RTL/packet_tx.v and RTL/pkt_rx.v).

Frame, 96 bits MSB first:
    preamble 0xAA (8) | sync 0x2DD4 (16) | box_id (32) | status (8) | sample (8) | seq (8) | CRC16 (16)
status = {stage[1:0], sensor_type[2:0], batt_low, query_flag, ack_flag}
CRC-16/CCITT-FALSE over the 7 payload bytes (poly 0x1021, init 0xFFFF).
"""
from dataclasses import dataclass

PREAMBLE = 0xAA
SYNC = 0x2DD4
FRAME_BYTES = 12
STAGES = {0: "FRESH", 1: "AGING", 2: "CRITICAL"}

# gateway (RTL tag_table) event codes
EVENT_NAMES = {0: "NONE", 1: "NEW_TAG", 2: "HEARTBEAT", 3: "STAGE_UP", 4: "ALERT_REPEAT",
               5: "RESTART", 6: "QUERY", 7: "LOST", 8: "RECOVERED", 9: "ANOMALY",
               10: "TABLE_FULL", 11: "ACK"}


def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass(frozen=True)
class Packet:
    box_id: int
    stage: int
    sensor_type: int
    batt_low: bool
    query: bool
    ack: bool
    sample: int          # temperature code (or restart counter when ack=True)
    seq: int
    rssi: int = 0

    @property
    def stage_name(self) -> str:
        return STAGES.get(self.stage, "?")

    @property
    def status_byte(self) -> int:
        return (self.stage << 6) | (self.sensor_type << 3) | (int(self.batt_low) << 2) \
               | (int(self.query) << 1) | int(self.ack)


def build_frame(p: Packet) -> bytes:
    """Exactly what packet_tx.v serialises (used by simulator and tests)."""
    payload = p.box_id.to_bytes(4, "big") + bytes([p.status_byte, p.sample & 0xFF, p.seq & 0xFF])
    return bytes([PREAMBLE]) + SYNC.to_bytes(2, "big") + payload + crc16_ccitt(payload).to_bytes(2, "big")


class FrameError(ValueError):
    pass


def _from_payload(payload: bytes, rssi: int = 0) -> Packet:
    box_id = int.from_bytes(payload[0:4], "big")
    st = payload[4]
    return Packet(box_id=box_id, stage=(st >> 6) & 3, sensor_type=(st >> 3) & 7,
                  batt_low=bool(st & 4), query=bool(st & 2), ack=bool(st & 1),
                  sample=payload[5], seq=payload[6], rssi=rssi)


def parse_frame(raw: bytes, rssi: int = 0) -> Packet:
    """Validate and decode a raw 12-byte over-the-air frame. Raises FrameError."""
    if len(raw) != FRAME_BYTES:
        raise FrameError(f"length {len(raw)} != {FRAME_BYTES}")
    if int.from_bytes(raw[1:3], "big") != SYNC:
        raise FrameError("bad sync word")
    payload, crc = raw[3:10], int.from_bytes(raw[10:12], "big")
    if crc16_ccitt(payload) != crc:
        raise FrameError("CRC mismatch")
    return _from_payload(payload, rssi)


# ---- gateway RTL -> host UART record (RTL/host_if.v) ----------------------------------
@dataclass(frozen=True)
class HostRecord:
    event: int
    packet: Packet
    gap: int

    @property
    def event_name(self) -> str:
        return EVENT_NAMES.get(self.event, "?")


def build_host_record(event: int, p: Packet, gap: int = 0) -> bytes:
    body = bytes([event & 0xF]) + p.box_id.to_bytes(4, "big") + bytes(
        [p.status_byte, p.sample, p.seq, gap & 0xFF, p.rssi & 0xFF])
    chk = 0
    for b in body:
        chk ^= b
    return b"\x7e" + body + bytes([chk])


def parse_host_record(rec: bytes) -> HostRecord:
    if len(rec) != 12 or rec[0] != 0x7E:
        raise FrameError("bad host record framing")
    chk = 0
    for b in rec[1:11]:
        chk ^= b
    if chk != rec[11]:
        raise FrameError("host record checksum mismatch")
    pk = _from_payload(rec[2:6] + bytes([rec[6], rec[7], rec[8]]), rssi=rec[10])
    return HostRecord(event=rec[1] & 0xF, packet=pk, gap=rec[9])
