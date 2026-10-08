"""expressions.py — the custom-expression library and the BLE upload path.

This is the feature the whole project exists for: give each hook state its own
face without reflashing. The firmware side has supported it since the rewrite,
but the upload path has never been exercised, so this module ships with a
built-in test expression and a CLI that can drive it end to end.

Upload protocol, and the reason it looks the way it does:

    EXPR_BEGIN(slot, len, crc16)   declare the blob; device allocates its buffer
    EXPR_CHUNK(slot, off, data)    stream it, in order, at most mtu-derived each
    EXPR_COMMIT(slot)              device verifies CRC16, then writes to flash

BEGIN and CHUNK are acknowledged only on failure — the device has nothing useful
to say about a chunk it accepted, and an ACK per chunk would halve throughput on
a link that can already only do ~10 KB/s. COMMIT always answers, and its code
distinguishes the failure modes: crc, nospace, or badreq (which is also what a
rejected BEGIN or an out-of-order CHUNK surfaces as, since either clears the
upload state machine).
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import protocol as P
from .ble_link import BleLink
from .config import expressions_dir
from .logbus import log

# Slot count and blob ceiling, duplicated from firmware/config.h. If these drift
# the device rejects the upload with ACK_BADREQ and nothing else explains why.
SLOT_COUNT = 12
EXPR_MAX_BYTES = 4096


@dataclass(slots=True)
class UploadResult:
    ok: bool
    slot: int
    bytes_sent: int = 0
    error: str | None = None
    ack_code: int | None = None


_ACK_NAMES = {
    P.ACK_OK: "ok",
    P.ACK_CRC: "crc mismatch",
    P.ACK_NOSPACE: "device out of room",
    P.ACK_BADREQ: "rejected (bad slot, length, or chunk order)",
}


class ExpressionLibrary:
    """On-disk library plus slot bookkeeping.

    The device is authoritative for what is actually stored; this manifest
    exists so the UI can render a list without a round trip per slot. A slot
    whose file is missing here but present on the device shows as "unknown" and
    can be cleared.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or expressions_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / "manifest.json"
        # slot -> {id, name, bytes}
        self.slots: dict[int, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.slots = {}
            return
        if not isinstance(raw, dict):
            return
        slots = raw.get("slots")
        if isinstance(slots, dict):
            for key, value in slots.items():
                try:
                    idx = int(key)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < SLOT_COUNT and isinstance(value, dict):
                    self.slots[idx] = value

    def save(self) -> None:
        payload = {"version": 1,
                   "slots": {str(k): v for k, v in sorted(self.slots.items())}}
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.manifest_path)

    # ── library ────────────────────────────────────────────────
    def put(self, expr_id: str, name: str, data: bytes) -> None:
        (self.dir / f"{expr_id}.json").write_bytes(data)
        for slot, meta in list(self.slots.items()):
            if meta.get("id") == expr_id:
                meta.update({"name": name, "bytes": len(data)})

    def get(self, expr_id: str) -> bytes | None:
        path = self.dir / f"{expr_id}.json"
        if not path.exists():
            return None
        return path.read_bytes()

    def delete(self, expr_id: str) -> None:
        path = self.dir / f"{expr_id}.json"
        if path.exists():
            path.unlink()
        for slot in [s for s, m in self.slots.items() if m.get("id") == expr_id]:
            self.forget_slot(slot)

    def list(self) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.dir.glob("*.json")):
            if path.name == "manifest.json":
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            out.append({"id": path.stem, "bytes": size})
        return out

    # ── slots ──────────────────────────────────────────────────
    def claim_free_slot(self) -> int | None:
        used = set(self.slots)
        for slot in range(SLOT_COUNT):
            if slot not in used:
                return slot
        return None

    def assign(self, slot: int, expr_id: str, name: str, size: int) -> None:
        self.slots[slot] = {"id": expr_id, "name": name, "bytes": size}
        self.save()

    def forget_slot(self, slot: int) -> bool:
        if slot in self.slots:
            del self.slots[slot]
            self.save()
            return True
        return False

    def slot_of(self, expr_id: str) -> int | None:
        for slot, meta in self.slots.items():
            if meta.get("id") == expr_id:
                return slot
        return None


# ── upload ───────────────────────────────────────────────────────────────────
async def upload(
    link: BleLink,
    slot: int,
    data: bytes,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> UploadResult:
    """Stream one expression blob into a device slot.

    on_progress(sent, total) is called after each chunk so a UI can show a bar;
    it is awaited inline, which is fine because the upload is already paced by
    the BLE link.
    """
    if not link.connected:
        return UploadResult(ok=False, slot=slot, error="device not connected")
    if slot >= SLOT_COUNT:
        return UploadResult(ok=False, slot=slot, error=f"slot {slot} out of range")
    if not data or len(data) > EXPR_MAX_BYTES:
        return UploadResult(ok=False, slot=slot,
                            error=f"blob is {len(data)} bytes, must be 1..{EXPR_MAX_BYTES}")

    total = len(data)
    crc = P.crc16_ccitt(data)

    # BEGIN. Deliberately NOT waiting for an ACK: the firmware acknowledges
    # BEGIN only when it rejects it, because a successful BEGIN has nothing to
    # report. Waiting here costs the full timeout on every single upload — which
    # is exactly what an earlier revision of this function did, producing
    # "ack timeout type=0x02" and a silent failure.
    #
    # A rejected BEGIN is not lost: it clears the upload state machine, so every
    # following CHUNK is refused and COMMIT answers ACK_BADREQ. The authoritative
    # verdict therefore comes from COMMIT, not from here.
    if not await link.send(P.MSG_EXPR_BEGIN,
                           P.struct_pack_begin(slot, total, crc)):
        return UploadResult(ok=False, slot=slot,
                            error="EXPR_BEGIN could not be written (link down?)")

    # CHUNKs. The budget comes from the negotiated MTU, never a constant: the
    # device drops a write larger than its ATT buffer and the upload then fails
    # at COMMIT with a length mismatch that looks like corruption.
    budget = P.chunk_payload_budget(link.mtu)
    offset = 0
    sent = 0
    while offset < total:
        piece = data[offset:offset + budget]
        ok = await link.send(P.MSG_EXPR_CHUNK,
                             P.struct_pack_chunk(slot, offset, piece))
        if not ok:
            return UploadResult(ok=False, slot=slot, bytes_sent=sent,
                                error="write failed mid-upload")
        offset += len(piece)
        sent += len(piece)
        if on_progress is not None:
            # on_progress may be sync (a CLI) or async (the HTTP endpoint's
            # WebSocket broadcast). Calling an async one without awaiting here
            # would silently drop every progress update and leak a coroutine,
            # so resolve it properly either way.
            outcome = on_progress(sent, total)
            if inspect.isawaitable(outcome):
                await outcome
        # Yield so the heartbeat and notification handling keep running; a
        # 4 KB blob at this link's rate is a noticeable slice of the loop
        # otherwise.
        await asyncio.sleep(0)

    # COMMIT
    commit = await link.send_frame(P.MSG_EXPR_COMMIT, P.struct_pack_commit(slot),
                                   wait_ack=True, ack_timeout=5.0)
    if commit is None:
        return UploadResult(ok=False, slot=slot, bytes_sent=sent,
                            error="no ack for EXPR_COMMIT")
    if commit.code != P.ACK_OK:
        return UploadResult(ok=False, slot=slot, bytes_sent=sent,
                            ack_code=commit.code,
                            error=_ACK_NAMES.get(commit.code, f"ack {commit.code}"))

    log.info("expression uploaded: slot=%d bytes=%d mtu=%d", slot, total, link.mtu)
    return UploadResult(ok=True, slot=slot, bytes_sent=sent)


async def select(link: BleLink, state: int, slot: int) -> bool:
    """Bind a state to a slot. 0xFF means "use the built-in face".

    Waits for the device's ACK, because the caller cannot tell "rejected" from
    "applied" any other way: the firmware bounds-checks the slot, and a slot
    expression that fails to parse is caught on load without an ACK. Without
    waiting, every binding was reported as a success — including unbinds, which
    the firmware used to refuse outright.
    """
    if state >= P.ST_COUNT:
        return False
    # A valid slot is 0..SLOT_COUNT-1, so the unbind sentinel has to live above
    # that range. Anything else above SLOT_COUNT is a bad index, not a sentinel.
    if slot >= SLOT_COUNT and slot != 0xFF:
        return False

    ack = await link.send_frame(P.MSG_EXPR_SELECT, bytes((state, slot)),
                                wait_ack=True, ack_timeout=5.0)
    if ack is None:
        log.warning("select state=%d slot=%d: no ack", state, slot)
        return False
    if ack.code != P.ACK_OK:
        log.warning("select state=%d slot=%d rejected: %s",
                    state, slot, _ACK_NAMES.get(ack.code, f"ack {ack.code}"))
        return False
    return True


async def config(link: BleLink, brightness: int, speed: int, rotation: int,
                 idle_s: int) -> bool:
    return await link.send_frame(
        P.MSG_CONFIG,
        bytes((brightness & 0xFF, speed & 0xFF, rotation & 0xFF, idle_s & 0xFF)))


# ── built-in test expression ─────────────────────────────────────────────────
# Deliberately unlike any built-in face: a large pale circle that pulses, so a
# successful upload is unmistakable on the panel.
TEST_EXPRESSION = {
    "schema": 1,
    "id": "test",
    "name": "Upload Test",
    "bg": "#0A0C10",
    "layers": [
        {"type": "circle", "cx": 120, "cy": 120, "r": 60,
         "color": "#5AC8FA", "effect": "pulse", "period_ms": 1400, "amount": 150},
        {"type": "rect", "x": 96, "y": 112, "w": 48, "h": 16, "color": "#FFFFFF"},
        {"type": "text", "x": 88, "y": 150, "size": 2,
         "text": "CUSTOM", "color": "#FFD60A"},
    ],
    "anim": {"fps": 20, "loop": True},
}


def _selftest() -> None:
    """Validate the packing helpers against the firmware's payload layout."""
    import struct

    # EXPR_BEGIN is slot + u16 len + u16 crc16 = 5 bytes.
    packed = P.struct_pack_begin(3, 1000, 0xABCD)
    assert len(packed) == 5, packed
    assert packed[0] == 3
    assert struct.unpack("<H", packed[1:3])[0] == 1000
    assert struct.unpack("<H", packed[3:5])[0] == 0xABCD

    # EXPR_CHUNK is slot + u16 offset + data.
    packed = P.struct_pack_chunk(2, 400, b"hello")
    assert packed[0] == 2
    assert struct.unpack("<H", packed[1:3])[0] == 400
    assert packed[3:] == b"hello"

    # EXPR_COMMIT is just the slot.
    assert P.struct_pack_commit(7) == bytes((7,))

    # Budget must respect both caps: the ATT write limit and MAX_PAYLOAD. At
    # this device's 519 MTU it is MAX_PAYLOAD that binds, which is why a
    # 346-byte blob no longer fits in a single chunk and the chunk loop (plus
    # the firmware's offset check) actually gets exercised.
    assert P.chunk_payload_budget(23) == 9, P.chunk_payload_budget(23)
    assert P.chunk_payload_budget(519) == P.MAX_PAYLOAD - 3, P.chunk_payload_budget(519)

    # The test expression must be small enough to upload at all.
    blob = json.dumps(TEST_EXPRESSION, separators=(",", ":")).encode("utf-8")
    assert len(blob) < EXPR_MAX_BYTES, len(blob)
    print(f"expressions selftest OK (test blob is {len(blob)} bytes)")


if __name__ == "__main__":
    _selftest()
