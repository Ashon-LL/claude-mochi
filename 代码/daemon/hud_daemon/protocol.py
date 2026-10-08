"""protocol.py — daemon <-> firmware wire codec.

The single shared contract with firmware/claude_hud/config.h. If the two sides
disagree, the symptom is total: every frame is rejected as garbage and the
device never acknowledges anything. So the CRC primitives below are pinned to
their published check values, and every builder/parser has a matching pair.

Layout, all fields little-endian:

    +------+------+-----+------+-----+--------+--------+---------+-----+
    | 0xA5 | 0x5A | VER | TYPE | SEQ | LEN_LO | LEN_HI | PAYLOAD | CRC8|
    +------+------+-----+------+-----+--------+--------+---------+-----+
    CRC8 covers the whole span above except the two SOF bytes.

SOF is excluded from the CRC so a resynchronising parser can hunt for the frame
start without knowing its length.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator

# ── Frame ────────────────────────────────────────────────────────────────────
SOF0 = 0xA5
SOF1 = 0x5A
VER = 0x01
HDR = 7
MAX_PAYLOAD = 256

# ── Messages ─────────────────────────────────────────────────────────────────
MSG_STATE = 0x01
MSG_EXPR_BEGIN = 0x02
MSG_EXPR_CHUNK = 0x03
MSG_EXPR_COMMIT = 0x04
MSG_EXPR_SELECT = 0x05
MSG_CONFIG = 0x06
MSG_PING = 0x07
MSG_TIME_SYNC = 0x08

MSG_PONG = 0x10
MSG_ACK = 0x11
MSG_STATUS = 0x12
MSG_LOG = 0x13

# Boot-animation upload, the same three-phase shape as the expression upload.
# Must match config.h exactly: an ID that means one thing on one side produces a
# link that is up and a device that answers nothing.
MSG_BOOT_BEGIN = 0x20
MSG_BOOT_CHUNK = 0x21
MSG_BOOT_COMMIT = 0x22
MSG_BOOT_PLAY = 0x23

# Which boot file a upload targets. The firmware writes them to LittleFS.
BOOT_TARGET_SEGS = 0
BOOT_TARGET_TRIS = 1
BOOT_TARGET_META = 2

# Segments are 8 bytes and triangles 12; the ceiling in config.h is 8192.
BOOT_MAX_BYTES = 8192

# ── States — must match HudState in firmware/config.h ────────────────────────
ST_IDLE = 0
ST_THINKING = 1
ST_TOOL_START = 2
ST_TOOL_END = 3
ST_WAITING = 4
ST_ERROR = 5
ST_OFFLINE = 6
ST_COUNT = 7

STATE_NAMES = {
    ST_IDLE: "idle",
    ST_THINKING: "thinking",
    ST_TOOL_START: "tool_start",
    ST_TOOL_END: "tool_end",
    ST_WAITING: "waiting",
    ST_ERROR: "error",
    ST_OFFLINE: "offline",
}

# Reverse lookup for the HTTP API, which takes state names from the UI.
STATE_NAMES_INV = {v: k for k, v in STATE_NAMES.items()}

# ACK codes
ACK_OK = 0
ACK_CRC = 1
ACK_NOSPACE = 2
ACK_BADREQ = 3

SLOT_COUNT = 12
EXPR_MAX_BYTES = 4096


# ── CRC ──────────────────────────────────────────────────────────────────────
def _build_crc8_table() -> list[int]:
    # CRC-8/SMBUS: poly 0x07, init 0x00, no reflection, no final xor.
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) if (crc & 0x80) else (crc << 1)
            crc &= 0xFF
        table.append(crc)
    return table


_CRC8_TABLE = _build_crc8_table()


def crc8(data: bytes, seed: int = 0x00) -> int:
    """CRC-8/SMBUS. Check value for b"123456789" is 0xF4."""
    crc = seed
    for byte in data:
        crc = _CRC8_TABLE[crc ^ byte]
    return crc


def crc16_ccitt(data: bytes, seed: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection.

    Check value for b"123456789" is 0x29B1. binascii.crc_hqx is the same
    function and is used for speed once the working is proven.
    """
    crc = seed
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc


# ── Frame model ──────────────────────────────────────────────────────────────
@dataclass(slots=True)
class Frame:
    type: int
    seq: int
    payload: bytes = b""


def encode(msg_type: int, seq: int, payload: bytes = b"") -> bytes:
    """Build one frame. Raises on an oversized payload rather than silently
    truncating: a short frame the device rejects is far easier to diagnose than
    one it accepts into a wrong buffer."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload {len(payload)} exceeds {MAX_PAYLOAD}")
    # Layout after the 2 SOF bytes: VER, TYPE, SEQ, LEN (u16 LE), PAYLOAD.
    body = struct.pack("<BBBH", VER, msg_type, seq, len(payload)) + payload
    # CRC covers VER..PAYLOAD, i.e. everything after the two SOF bytes.
    return bytes((SOF0, SOF1)) + body + bytes((crc8(body),))


@dataclass(slots=True)
class DecodeResult:
    """Outcome of feeding bytes to FrameDecoder."""
    frame: Frame | None = None
    consumed: int = 0       # bytes eaten, valid when frame is not None
    error: str | None = None  # why bytes were dropped


class FrameDecoder:
    """Incremental parser for the notification stream.

    Mirrors the firmware's own strategy: buffer, try a frame at offset 0, and
    on mismatch drop exactly one byte rather than discarding the buffer. A
    corrupted chunk therefore costs one byte of resync, not the connection.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        """Buffer bytes, then drain every complete frame.

        Returns a list, not a generator: the buffering must happen on entry,
        and a generator defers even that until first iteration — which silently
        drops bytes when a caller does `list(d.feed(part))` and discards the
        result, or forgets to consume it at all.
        """
        self._buf += data
        out: list[Frame] = []
        while True:
            result = self._try_one()
            if result.frame is not None:
                out.append(result.frame)
                del self._buf[: result.consumed]
                continue
            # frame is None means one of:
            #   consumed == 0  -> need more bytes, stop and wait
            #   error is set   -> a byte was dropped, try again immediately
            # Without the second case the loop would bail out on the first
            # garbage byte and leave the rest of the stream unparsed forever.
            if result.error is None:
                return out

    # Kept for symmetry with the firmware's drain loop and for callers that
    # prefer pull-style reads.
    def next_frame(self) -> Frame | None:
        if not self._buf:
            return None
        result = self._try_one()
        return result.frame

    def _try_one(self) -> DecodeResult:
        buf = self._buf
        if len(buf) < HDR + 1:
            return DecodeResult()
        if buf[0] != SOF0 or buf[1] != SOF1:
            # Not a frame start; drop one byte and let the caller retry.
            del buf[:1]
            return DecodeResult(error="bad_sof")
        if buf[2] != VER:
            del buf[:1]
            return DecodeResult(error="bad_ver")

        length = struct.unpack_from("<H", buf, 5)[0]
        if length > MAX_PAYLOAD:
            del buf[:1]
            return DecodeResult(error="bad_len")

        total = HDR + length + 1
        if len(buf) < total:
            # Prefix of a plausible frame: keep buffering, drop nothing.
            return DecodeResult()

        want = crc8(bytes(buf[2 : HDR + length]))
        if buf[HDR + length] != want:
            del buf[:1]
            return DecodeResult(error="bad_crc")
        if buf[HDR + length] != want:
            del buf[:1]
            return DecodeResult(error="bad_crc")

        msg_type, seq = buf[3], buf[4]
        payload = bytes(buf[HDR : HDR + length])
        return DecodeResult(frame=Frame(msg_type, seq, payload), consumed=total)

    def reset(self) -> None:
        self._buf.clear()


# ── Host -> device builders ──────────────────────────────────────────────────
def state_frame(state: int, seq: int) -> bytes:
    return encode(MSG_STATE, seq, struct.pack("<B", state))


def expr_begin(slot: int, length: int, crc: int, seq: int) -> bytes:
    """slot, u16 len, u16 crc16 — 5 bytes, matching the firmware payload.

    DESIGN.md §5.1 also lists a raw_len field; the firmware build dropped it
    because the device never needed it (the blob is compressed-or-not JSON it
    parses in place). One source of truth wins: this file is it.
    """
    return encode(MSG_EXPR_BEGIN, seq, struct.pack("<BHH", slot, length, crc))


def expr_chunk(slot: int, offset: int, data: bytes, seq: int) -> bytes:
    return encode(MSG_EXPR_CHUNK, seq, struct.pack("<BH", slot, offset) + data)


def expr_commit(slot: int, seq: int) -> bytes:
    return encode(MSG_EXPR_COMMIT, seq, bytes((slot,)))


def expr_select(state: int, slot: int, seq: int) -> bytes:
    return encode(MSG_EXPR_SELECT, seq, struct.pack("<BB", state, slot))


# ── Payload packers ──────────────────────────────────────────────────────────
# Separate from the frame builders above because the caller owns the sequence
# number when it is streaming a multi-message upload and needs to correlate
# the ACK. Packing the payload on its own keeps that possible.
def struct_pack_begin(slot: int, length: int, crc: int) -> bytes:
    """slot, u16 len, u16 crc16 — 5 bytes, matching the firmware payload.

    DESIGN.md §5.1 also lists a raw_len field; the firmware build dropped it
    because the device never needed it. One source of truth wins: this file is it.
    """
    return struct.pack("<BHH", slot, length, crc)


def struct_pack_chunk(slot: int, offset: int, data: bytes) -> bytes:
    return struct.pack("<BH", slot, offset) + data


def struct_pack_commit(slot: int) -> bytes:
    return bytes((slot,))


def struct_pack_ping_ts(ts_ms: int) -> bytes:
    """u32 timestamp, echoed back by the firmware in PONG.

    The firmware ignores a PING whose payload is shorter than 4 bytes, so a
    ping must always carry this.
    """
    return struct.pack("<I", ts_ms & 0xFFFFFFFF)


def config_frame(brightness: int, speed: int, rotation: int, idle_s: int, seq: int) -> bytes:
    return encode(MSG_CONFIG, seq, struct.pack("<BBBB", brightness, speed, rotation, idle_s))


def ping(ts_ms: int, seq: int) -> bytes:
    return encode(MSG_PING, seq, struct.pack("<I", ts_ms & 0xFFFFFFFF))


def chunk_payload_budget(mtu: int) -> int:
    """Largest data slice one EXPR_CHUNK can carry.

    Two independent limits, and both must be respected:

      * the ATT write is capped at mtu-3
      * the whole frame payload is capped at MAX_PAYLOAD, and an EXPR_CHUNK's
        payload is its own 3-byte sub-header plus the data, so the data may use
        at most MAX_PAYLOAD-3

    The second limit is the one that bites: at mtu 519 the ATT budget is 505,
    which is larger than MAX_PAYLOAD, so a blob under ~250 bytes fits in one
    chunk and silently produces an oversized payload. Clamping to the smaller
    of the two is the only correct answer.
    """
    att_room = mtu - 3 - HDR - 1 - 3
    if att_room < 1:
        # ATT floor: 23 leaves room for 9 data bytes. Uploads at that MTU are
        # slow but correct. A higher minimum would produce a frame larger than
        # the peer can accept, which is a silent data loss rather than a slow
        # transfer — so the floor stays at 1.
        return 1
    return min(att_room, MAX_PAYLOAD - 3)


# ── Device -> host parsers ───────────────────────────────────────────────────
@dataclass(slots=True)
class Pong:
    ts_ms: int
    fw_major: int
    fw_minor: int
    slot_count: int
    used_slots: int


@dataclass(slots=True)
class Ack:
    acked_type: int
    acked_seq: int
    code: int


@dataclass(slots=True)
class Status:
    state: int
    ble_connected: bool
    last_err: int


@dataclass(slots=True)
class LogLine:
    text: str


def parse_pong(payload: bytes) -> Pong | None:
    if len(payload) < 8:
        return None
    ts, maj, minr, slots, used = struct.unpack("<IBBBB", payload[:8])
    return Pong(ts, maj, minr, slots, used)


def parse_ack(payload: bytes) -> Ack | None:
    if len(payload) < 3:
        return None
    return Ack(payload[0], payload[1], payload[2])


def parse_status(payload: bytes) -> Status | None:
    if len(payload) < 3:
        return None
    return Status(payload[0], bool(payload[1]), payload[2])


def parse_log(payload: bytes) -> LogLine | None:
    if not payload:
        return None
    return LogLine(payload.decode("utf-8", errors="replace"))


# ── Self-test ────────────────────────────────────────────────────────────────
def _selftest() -> None:
    # Published check values. If these fail, the daemon and firmware disagree
    # about CRC and nothing else can be trusted.
    assert crc8(b"123456789") == 0xF4, hex(crc8(b"123456789"))
    assert crc16_ccitt(b"123456789") == 0x29B1, hex(crc16_ccitt(b"123456789"))

    # Round-trip through the decoder, including a frame split mid-stream.
    wire = state_frame(ST_THINKING, 7) + ping(1234, 8)
    dec = FrameDecoder()
    assert dec.feed(wire[:4]) == []          # too short: nothing yet
    got = dec.feed(wire[4:])
    assert [f.type for f in got] == [MSG_STATE, MSG_PING], [f.type for f in got]
    assert got[0].payload == bytes((ST_THINKING,))

    # Garbage in front of a good frame must cost exactly the garbage bytes.
    dec2 = FrameDecoder()
    noisy = b"\x00\xff\xa5" + ping(99, 1)
    got2 = dec2.feed(noisy)
    assert len(got2) == 1 and got2[0].type == MSG_PING, got2

    # A truncated frame waits for more bytes rather than erroring.
    dec3 = FrameDecoder()
    half = encode(MSG_STATUS, 1, b"abc")[:5]
    assert dec3.feed(half) == []
    rest = dec3.feed(encode(MSG_STATUS, 1, b"abc")[5:])
    assert [f.type for f in rest] == [MSG_STATUS], rest

    # Chunk budget must respect BOTH caps: the ATT write limit and the frame
    # payload limit. Asserting only "bigger than X" passes even when the MAX_
    # PAYLOAD clamp is missing, which is exactly how the 346-byte test blob
    # turned into a 349-byte payload and blew up. The invariant that matters is
    # that a chunk built from this budget always fits.
    for mtu in (23, 64, 185, 247, 517, 519, 1200):
        budget = chunk_payload_budget(mtu)
        assert budget >= 1, (mtu, budget)
        assert budget + 3 <= MAX_PAYLOAD, (mtu, budget)
        # And the resulting frame must actually encode at that MTU.
        blob = b"x" * budget
        frame = expr_chunk(0, 0, blob, 1)
        assert frame[0] == SOF0 and len(frame) == HDR + 3 + budget + 1, (mtu, len(frame))
        assert len(frame) - 3 <= mtu, (mtu, len(frame))

    # Sanity on the numbers themselves. 519 is what this device negotiates, and
    # MAX_PAYLOAD is the cap that binds there, not the ATT limit.
    assert chunk_payload_budget(23) == 9, chunk_payload_budget(23)
    assert chunk_payload_budget(519) == MAX_PAYLOAD - 3, chunk_payload_budget(519)
    # A MTU we cannot beat is clamped rather than wrapped.
    assert chunk_payload_budget(5) == 1, chunk_payload_budget(5)

    # ── every public constructor, exercised ──
    # A constant-name typo once shipped as MSG_EXPR_EXPR_SELECT and survived,
    # because no test called any of these functions. Both self-tests checked the
    # primitives underneath — struct packing, CRC, chunk budgets — and walked
    # straight past a typo in the layer that actually gets used. The rule that
    # falls out of it: every public function gets called at least once, even
    # when the interesting behaviour is somewhere else.
    sf = state_frame(ST_THINKING, 1)
    assert sf[0] == SOF0 and sf[1] == SOF1 and sf[2] == VER
    assert sf[3] == MSG_STATE and sf[4] == 1 and sf[7] == ST_THINKING

    eb = expr_begin(3, 1000, 0xABCD, 2)
    assert eb[3] == MSG_EXPR_BEGIN and eb[4] == 2
    assert struct.unpack("<BHH", eb[7:12]) == (3, 1000, 0xABCD)

    ec = expr_chunk(2, 400, b"hello", 3)
    assert ec[3] == MSG_EXPR_CHUNK and ec[4] == 3
    assert ec[7] == 2 and struct.unpack("<H", ec[8:10])[0] == 400
    # [10:] would include the trailing CRC byte; slice the data explicitly.
    assert ec[10:15] == b"hello" and len(ec) == HDR + 3 + 5 + 1

    ecm = expr_commit(7, 4)
    assert ecm[3] == MSG_EXPR_COMMIT and ecm[4] == 4 and ecm[7] == 7

    es = expr_select(ST_WAITING, 5, 5)
    assert es[3] == MSG_EXPR_SELECT and es[4] == 5
    assert es[7] == ST_WAITING and es[8] == 5

    cf = config_frame(160, 2, 1, 30, 6)
    assert cf[3] == MSG_CONFIG and cf[7:11] == bytes((160, 2, 1, 30))

    pg = ping(0xDEADBEEF, 7)
    assert pg[3] == MSG_PING and struct.unpack("<I", pg[7:11])[0] == 0xDEADBEEF

    # Each constructor must also survive a round trip through the decoder.
    for wire in (sf, eb, ec, ecm, es, cf, pg):
        dec = FrameDecoder()
        got = dec.feed(wire)
        assert len(got) == 1, wire.hex()
        assert got[0].type == wire[3] and got[0].seq == wire[4]


if __name__ == "__main__":
    _selftest()
    print("protocol selftest OK")
