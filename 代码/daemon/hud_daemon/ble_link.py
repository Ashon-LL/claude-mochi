"""ble_link.py — the daemon's sole owner of the BLE connection.

The daemon must be the only process that talks to the device. Claude Code
hooks, the Electron UI, and the CLI all reach it over localhost instead: a BLE
peripheral accepts one central connection at a time, and a second central
stealing it produces the worst possible symptom — the HUD goes dark with no
error anywhere.

This module owns scanning, connect, MTU discovery, notification subscription,
reconnect with backoff, frame sending, and ACK waiting. It knows nothing about
hook events or expressions.

Threading note that shapes the whole file: bleak's WinRT backend calls
notification and disconnect callbacks from its own worker thread, not from the
asyncio loop. Anything that touches asyncio state therefore goes through
call_soon_threadsafe, and only the pure byte-level frame decoding stays on the
callback thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import protocol as P
from .logbus import log

BACKOFF_MAX_S = 30.0
SCAN_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 10.0
ACK_TIMEOUT_S = 2.0

# Granularity of the "is the device still there?" poll inside a session. The
# drop signal is an Event set from bleak's thread, so this is only a backstop
# for stop(); a real disconnect wakes it immediately via the same Event.
SESSION_POLL_S = 0.25


def _loop_or_none() -> asyncio.AbstractEventLoop | None:
    """The running loop, or None when called off the event loop.

    bleak's worker thread has no running loop, which is exactly the case this
    guards. Returning None instead of raising keeps the callbacks readable.
    """
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class BleLink:
    def __init__(
        self,
        *,
        service_uuid: str,
        rx_uuid: str,
        tx_uuid: str,
        on_frame: Callable[[P.Frame], None] | None = None,
        on_state: Callable[[str], None] | None = None,
    ) -> None:
        self.service_uuid = service_uuid
        self.rx_uuid = rx_uuid
        self.tx_uuid = tx_uuid
        self._on_frame = on_frame
        self._on_state = on_state

        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: BleakClient | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._dropped = asyncio.Event()

        self._decoder = P.FrameDecoder()
        self._seq = 0
        self._acks: dict[int, asyncio.Future] = {}
        # seq -> host monotonic time at send. The device echoes our seq back on
        # PONG, so RTT is computed entirely from the host clock: device millis()
        # and host monotonic have no relationship to each other.
        self._ping_sent_at: dict[int, float] = {}
        self._backoff = 1.0

        self.state = "disconnected"
        self.address: str | None = None
        self.mtu = 23
        self.rtt_ms: float | None = None

    # ── lifecycle ──────────────────────────────────────────────
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._dropped.clear()
        self._task = asyncio.create_task(self._run(), name="ble-link")
        log.info("ble_link starting (service %s)", self.service_uuid)

    def attach_handlers(
        self,
        on_frame: Callable[[P.Frame], None] | None,
        on_state: Callable[[str], None] | None = None,
    ) -> None:
        """Replace the notification and link-state callbacks after construction.

        Needed because the consumer of device frames is the IPC server, which
        cannot be built until the link exists (it sends through it) — so the
        link is constructed bare and its handlers attached immediately
        afterwards, before start(). Safe exactly for that reason: no frame can
        arrive before the session begins, and start() is called after this.
        """
        self._on_frame = on_frame
        self._on_state = on_state

    async def stop(self) -> None:
        self._stop.set()
        self._dropped.set()          # wake the session poll
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._client = None
        self._set_state("disconnected")

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    def status(self) -> dict:
        return {
            "state": self.state,
            "address": self.address,
            "mtu": self.mtu,
            "rtt_ms": self.rtt_ms,
            "backoff_s": round(self._backoff, 1),
        }

    # ── sending ────────────────────────────────────────────────
    def next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFF
        return self._seq

    async def _write(self, msg_type: int, seq: int, payload: bytes) -> bool:
        client = self._client
        if client is None or not client.is_connected:
            log.debug("write skipped, link %s (type=0x%02x)", self.state, msg_type)
            return False
        wire = P.encode(msg_type, seq, payload)
        try:
            # write-without-response first: it is the only path fast enough for
            # a chunked expression upload. Not every adapter accepts it, so
            # fall back rather than failing the send.
            await client.write_gatt_char(self.rx_uuid, wire, response=False)
            return True
        except Exception:
            try:
                await client.write_gatt_char(self.rx_uuid, wire)
                return True
            except Exception as exc:
                log.warning("write failed type=0x%02x: %s", msg_type, exc)
                return False

    async def send(self, msg_type: int, payload: bytes = b"") -> bool:
        """Fire-and-forget send. True when the write left this machine.

        Separate from send_frame because that one returns None for two very
        different reasons — "no ACK was expected" and "the write failed" — and
        callers that only care about delivery cannot tell them apart. Returning
        a bool here makes "did it go out" answerable without inspecting the link.
        """
        if not self.connected:
            log.debug("send skipped, link %s (type=0x%02x)", self.state, msg_type)
            return False
        return await self._write(msg_type, self.next_seq(), payload)

    async def send_frame(
        self, msg_type: int, payload: bytes = b"", *, wait_ack: bool = False,
        ack_timeout: float = ACK_TIMEOUT_S,
    ) -> P.Ack | None:
        """Send one frame and, when asked, wait for the device's ACK.

        Returns the ACK when wait_ack is set, else None. None also means "no
        answer arrived" — timeout, or the link went away mid-flight — so callers
        must check self.connected to tell the two apart rather than parsing the
        return value for a reason code. Prefer send() when you do not need the
        ACK.
        """
        if not self.connected:
            return None

        seq = self.next_seq()
        if msg_type == P.MSG_PING:
            self._ping_sent_at[seq] = time.monotonic()

        fut: asyncio.Future | None = None
        if wait_ack:
            fut = asyncio.get_running_loop().create_future()
            self._acks[seq] = fut

        if not await self._write(msg_type, seq, payload):
            self._acks.pop(seq, None)
            return None

        if fut is None:
            return None
        try:
            return await asyncio.wait_for(fut, timeout=ack_timeout)
        except asyncio.TimeoutError:
            log.warning("ack timeout type=0x%02x seq=%d", msg_type, seq)
            return None
        finally:
            self._acks.pop(seq, None)

    async def ping(self) -> bool:
        """Send one PING. The matching PONG updates self.rtt_ms on its own.

        Deliberately non-blocking: the heartbeat loop fires these every few
        seconds and must not stall the event loop waiting for a reply.

        The payload carries the host's millisecond clock because the firmware
        echoes it back in PONG, which is what makes an RTT measurable on the
        device side. An empty payload is also rejected outright by the firmware
        (it requires at least 4 bytes), so a ping that sends nothing is a ping
        that never gets answered.
        """
        if not self.connected:
            return False
        seq = self.next_seq()
        self._ping_sent_at[seq] = time.monotonic()
        ts_ms = int(time.time() * 1000) & 0xFFFFFFFF
        return await self._write(P.MSG_PING, seq, P.struct_pack_ping_ts(ts_ms))

    # ── supervision ────────────────────────────────────────────
    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._attempt()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("link attempt failed: %s", exc)
            finally:
                self._client = None
                self._release_waiters()
                self._set_state("disconnected")
                self._decoder.reset()
            await self._backoff_sleep()

    async def _attempt(self) -> None:
        self._set_state("scanning")
        device = await self._scan()
        if device is None:
            log.debug("scan found nothing (service %s)", self.service_uuid)
            return

        self._set_state("connecting")
        log.info("connecting to %s", device.address)

        async with BleakClient(
            device, disconnected_callback=self._on_disconnected, timeout=CONNECT_TIMEOUT_S
        ) as client:
            self._client = client
            self._dropped.clear()
            await self._wire_connected(client)

            self._set_state("connected")
            self.reset_backoff()
            # A previous session's waiters are meaningless now; resolve them as
            # "no answer" so their callers do not hang until timeout.
            self._release_waiters()
            log.info("connected %s mtu=%d", client.address, self.mtu)

            await self._poll_session()

        log.info("link session ended")
        self._client = None

    async def _poll_session(self) -> None:
        """Hold the session open until stop() or a device drop.

        Polled rather than awaited on an Event because _dropped is set from
        bleak's worker thread, and mixing that with asyncio.wait on a bare Event
        is where the subtle races live. The poll is cheap and obviously correct.
        """
        while not self._stop.is_set() and not self._dropped.is_set():
            await asyncio.sleep(SESSION_POLL_S)

    async def _scan(self) -> BLEDevice | None:
        # Filter on the advertised service UUID, never the device name: any
        # nearby BLE speaker could be called "Claude-HUD", but nothing else
        # advertises our service.
        target = self.service_uuid.lower()
        try:
            return await BleakScanner.find_device_by_filter(
                lambda _d, adv: target in [u.lower() for u in (adv.service_uuids or [])],
                timeout=SCAN_TIMEOUT_S,
            )
        except Exception as exc:
            log.warning("scan failed: %s", exc)
            return None

    async def _wire_connected(self, client: BleakClient) -> None:
        self.address = client.address

        # Dump the GATT tree once at connect. It is the only way to see from the
        # log whether Windows served a cached service table from an older
        # firmware, which otherwise looks like "the characteristic vanished".
        tree = []
        for svc in client.services:
            for ch in svc.characteristics:
                tree.append(f"{svc.uuid[-12:]}/{ch.uuid[-12:]}[{','.join(ch.properties)}]")
        log.debug("gatt: %s", " ".join(tree))

        for svc in client.services:
            for ch in svc.characteristics:
                if ch.uuid.lower() == self.tx_uuid.lower():
                    await client.start_notify(ch.uuid, self._on_notify)
                    log.debug("notify subscribed on %s", self.tx_uuid)
                    break
            else:
                continue
            break

        # Windows exposes the negotiated MTU only after connect, and it does not
        # follow the 517 we ask for. Read what we actually got: every chunk size
        # and frame budget depends on it.
        try:
            self.mtu = int(client.mtu_size)
        except Exception:
            self.mtu = 23
        log.info("negotiated mtu=%d", self.mtu)

    # ── callbacks, running on bleak's worker thread ────────────
    def _on_disconnected(self, _client: BleakClient) -> None:
        log.info("device dropped")
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._dropped.set)

    def _on_notify(self, _sender, data: bytearray) -> None:
        # Decoding is pure and thread-safe, so it stays here and does not block
        # bleak's thread with a loop hop.
        frames = self._decoder.feed(bytes(data))
        if not frames:
            return
        running = _loop_or_none()
        if running is not None and running is self._loop:
            self._emit(frames)
        elif self._loop is not None:
            self._loop.call_soon_threadsafe(self._emit, frames)
        else:
            log.warning("notification before loop ready, %d bytes dropped", len(data))

    # ── on the event loop ──────────────────────────────────────
    def _emit(self, frames: list[P.Frame]) -> None:
        for frame in frames:
            if frame.type == P.MSG_ACK:
                ack = P.parse_ack(frame.payload)
                if ack is not None:
                    fut = self._acks.get(ack.acked_seq)
                    if fut is not None and not fut.done():
                        fut.set_result(ack)
                return

            if frame.type == P.MSG_PONG:
                pong = P.parse_pong(frame.payload)
                if pong is not None:
                    sent_at = self._ping_sent_at.pop(frame.seq, None)
                    if sent_at is not None:
                        self.rtt_ms = round((time.monotonic() - sent_at) * 1000.0, 1)

            if self._on_frame is not None:
                try:
                    self._on_frame(frame)
                except Exception as exc:
                    log.warning("frame handler error: %s", exc)

    def _release_waiters(self) -> None:
        """Resolve pending ACK waiters with None ("no answer") and clear RTT.

        Must run on the event loop. Resolving rather than raising keeps
        send_frame's await clean, and avoids asyncio's "exception was never
        retrieved" warnings from futures nobody is watching any more.
        """
        for fut in list(self._acks.values()):
            if not fut.done():
                fut.set_result(None)
        self._acks.clear()
        self._ping_sent_at.clear()

    # ── helpers ────────────────────────────────────────────────
    def _set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        if self._on_state is not None:
            try:
                self._on_state(state)
            except Exception as exc:
                log.debug("state listener error: %s", exc)

    async def _backoff_sleep(self) -> None:
        delay = self._backoff
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    def reset_backoff(self) -> None:
        self._backoff = 1.0
