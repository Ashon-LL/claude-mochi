"""Smoke test for the feedback-loop fixes. Run from anywhere.

Verifies the four things that changed in the feedback path:
  1. EVENT_FIELDS is defined (its absence crashed the hook worker outright)
  2. BleLink can be built bare and have its handlers attached later
  3. bind_device_handlers routes frames into the IPC server's broadcaster
  4. select() accepts the 0xFF unbind sentinel and rejects bad indices

Prints one line per check so a failure is obvious without a traceback hunt.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, r'D:\Claude DIY\代码\daemon')

from hud_daemon import ipc_server as S
from hud_daemon import protocol as P
from hud_daemon.expressions import select, SLOT_COUNT
from hud_daemon import ble_link as BL

results = []


def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))


# 1 ── the crash fix
check('EVENT_FIELDS defined and contains ev/hook_event_name',
      'ev' in S.EVENT_FIELDS and 'hook_event_name' in S.EVENT_FIELDS,
      str(S.EVENT_FIELDS))

# 2 ── bare construction + late attach
link = BL.BleLink(service_uuid='x', rx_uuid='y', tx_uuid='z')
check('BleLink builds with no handlers', link._on_frame is None)

# 3 ── STATUS broadcast payload shape
frame = P.Frame(P.MSG_STATUS, 0, bytes((P.ST_THINKING, 1, 0)))
st = P.parse_status(frame.payload)
check('parse_status reads state/ble/err',
      st.state == P.ST_THINKING and st.ble_connected is True and st.last_err == 0,
      f'{st.state}/{st.ble_connected}/{st.last_err}')


class BroadcastSpy(S.IpcServer):
    """Captures the payloads handle_device_frame would broadcast.

    _spawn is replaced with a synchronous drain rather than a no-op: the real
    one hands the coroutine to the event loop, and closing it without awaiting
    would silently discard every payload — which is exactly the failure mode
    this test is supposed to rule out.
    """

    def __init__(self):
        self._bg_tasks = set()
        self.seen = []

    def _spawn(self, coro):
        asyncio.run(coro)
        self._bg_tasks.clear()

    async def _broadcast(self, payload):
        self.seen.append(payload)


spy = BroadcastSpy()
spy.bind_device_handlers(link)
# The frame handler must now be the IPC server's own method, not the bare
# placeholder BleLink was constructed with.
check('frame handler is IpcServer.handle_device_frame',
      link._on_frame.__func__.__name__ == 'handle_device_frame',
      getattr(link._on_frame, '__func__', link._on_frame).__name__)

# Drop a real STATUS frame through it and confirm the broadcast payload.
link._on_frame(P.Frame(P.MSG_STATUS, 0, bytes((P.ST_THINKING, 1, 0))))
check('STATUS frame becomes a device-status broadcast',
      any(p.get('type') == 'device-status' and p.get('state_name') == 'thinking'
          and p.get('ble') is True for p in spy.seen),
      repr(spy.seen))

# A PONG carries the firmware version, which nothing else exposes.
# Payload is u32 ts_ms, then fw_major, fw_minor, slot_count, used_slots.
link._on_frame(P.Frame(P.MSG_PONG, 0, bytes((0, 0, 0, 0, 1, 0, 12, 3))))
check('PONG frame becomes a device-info broadcast',
      any(p.get('type') == 'device-info' and p.get('fw') == '1.0'
          and p.get('used_slots') == 3 for p in spy.seen),
      repr(spy.seen))

# A failed ACK must not be swallowed: it is the only evidence a rejected
# upload ever reaches the user.
link._on_frame(P.Frame(P.MSG_ACK, 0, bytes((P.MSG_EXPR_COMMIT, 7, P.ACK_CRC))))
check('bad ACK becomes a device-ack-error broadcast',
      any(p.get('type') == 'device-ack-error' and p.get('ack_name') == 'crc'
          for p in spy.seen),
      repr(spy.seen))

# An OK ACK is the absence of news and must stay silent.
before = len(spy.seen)
link._on_frame(P.Frame(P.MSG_ACK, 0, bytes((P.MSG_EXPR_COMMIT, 8, P.ACK_OK))))
check('ACCEPTED ACK is not broadcast', len(spy.seen) == before)

# Device-reported trouble over MSG_LOG must reach the UI too.
link._on_frame(P.Frame(P.MSG_LOG, 0, b'slot: 2 rejected (346 bytes): device out of memory'))
check('device failure log is broadcast',
      any(p.get('type') == 'device-log' for p in spy.seen),
      repr(spy.seen))

# A routine INFO log line is not "trouble" and must not be pushed.
before = len(spy.seen)
link._on_frame(P.Frame(P.MSG_LOG, 0, b'store:mounted'))
check('routine device log is not broadcast', len(spy.seen) == before)

# link state changes are pushed the moment they happen, not on the next poll.
spy.seen.clear()
link._on_state('scanning')
check('link state change is broadcast',
      spy.seen == [{'type': 'link', 'state': 'scanning'}], repr(spy.seen))


# 4 ── select() sentinel handling
class OkLink:
    connected = True
    mtu = 519

    async def send_frame(self, t, payload, *a, wait_ack=False, **k):
        return P.Ack(t, 0, P.ACK_OK)


class BadReqLink(OkLink):
    async def send_frame(self, t, payload, *a, wait_ack=False, **k):
        return P.Ack(t, 0, P.ACK_BADREQ)


async def _main():
    check('select(0xFF) unbind accepted', await select(OkLink(), P.ST_THINKING, 0xFF))
    check('select(SLOT_COUNT-1) accepted', await select(OkLink(), P.ST_THINKING, SLOT_COUNT - 1))
    check(f'select({SLOT_COUNT}) rejected', not await select(OkLink(), P.ST_THINKING, SLOT_COUNT))
    check('select(200) rejected (not a sentinel)',
          not await select(OkLink(), P.ST_THINKING, 200))
    check('select(state=99) rejected', not await select(OkLink(), 99, 0))
    check('ACK_BADREQ surfaces as False', not await select(BadReqLink(), P.ST_THINKING, 3))


asyncio.run(_main())

# ── report ───────────────────────────────────────────────────────────
width = max(len(n) for n, _, _ in results)
failed = 0
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    if not ok:
        failed += 1
print(f"\n{len(results) - failed}/{len(results)} passed")
sys.exit(1 if failed else 0)
