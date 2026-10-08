#!/usr/bin/env python3
"""cchud-hook — the program Claude Code actually runs on every hook event.

Design constraints, in priority order:

  1. It must never block Claude Code. Every failure path exits 0 immediately.
     Claude Code treats a non-zero hook exit as a hook error, and a hung hook
     stalls the whole turn — so "do nothing" is always the right answer here.
  2. It must start fast. It runs once per tool call, so its startup cost is paid
     on every single tool invocation. Hence: stdlib only, no third-party imports.
  3. It must never wait on the network.

That third one is why this speaks UDP rather than HTTP. A TCP connect to a dead
port does not fail fast on every machine: security software and VPN clients
install filtering rules that silently drop loopback SYNs instead of refusing
them, which makes connect() block for the full timeout. Measured on this
machine: 266 ms of dead air per tool call with the daemon stopped. A UDP
datagram to localhost has no handshake, cannot block, and costs well under a
millisecond even when nothing is listening.

For a HUD that is the right trade: a dropped animation frame is invisible, a
266 ms stall on every tool call is not.
"""

from __future__ import annotations

import json
import socket
import sys

HOST = "127.0.0.1"
PORT = 17321

# A datagram must fit in one packet. Our payload is ~120 bytes; the cap exists
# only so a pathological payload cannot be silently truncated by the kernel.
MAX_DATAGRAM = 1400

# Refuse absurd payloads outright: a multi-megabyte stdin would otherwise be
# read into memory just to be thrown away.
STDIN_MAX = 1 << 20

SENTINEL = "cchud-hook"


def _send(payload: bytes) -> bool:
    """Fire one datagram at the daemon. Never blocks, never raises."""
    if len(payload) > MAX_DATAGRAM:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No connect(): a connected UDP socket would still resolve and could
        # inherit the same filtering delay we are avoiding.
        sock.sendto(payload, (HOST, PORT))
        return True
    except OSError:
        # Nothing listening, or the payload was rejected locally. Either way
        # there is no retry worth making.
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _handle(stdin_bytes: bytes) -> int:
    if not stdin_bytes:
        return 0

    try:
        data = json.loads(stdin_bytes)
    except (ValueError, UnicodeDecodeError):
        return 0
    if not isinstance(data, dict):
        return 0

    event = data.get("hook_event_name")
    if not isinstance(event, str) or not event:
        return 0

    # Reduce before sending: tool_input and tool_response are the parts that can
    # carry an entire file's contents, and the daemon never reads them.
    tool = data.get("tool_name")
    sid = data.get("session_id")
    payload = json.dumps(
        {
            "v": 1,
            "ev": event,
            "tool": tool if isinstance(tool, str) else None,
            "sid": sid if isinstance(sid, str) else None,
            "src": SENTINEL,
        },
        separators=(",", ":"),
    ).encode("utf-8")

    _send(payload)
    return 0


def _selftest() -> int:
    """Send a synthetic event and report the cost of one hook invocation."""
    import time

    payload = json.dumps({"v": 1, "ev": "PreToolUse", "tool": "Bash",
                          "sid": "selftest", "src": SENTINEL},
                         separators=(",", ":")).encode()
    start = time.perf_counter()
    ok = _send(payload)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    print(f"UDP -> {HOST}:{PORT} {'sent' if ok else 'failed'} in {elapsed_ms:.2f} ms")
    if not ok:
        print("(send failed — nothing is wrong with the daemon; see the docstring)")
    return 0


def main(argv: list[str]) -> int:
    if "--test" in argv:
        return _selftest()

    try:
        stdin_bytes = sys.stdin.buffer.read(STDIN_MAX)
    except BaseException:
        return 0
    return _handle(stdin_bytes)


if __name__ == "__main__":
    # Guard the whole thing: nothing this shim does is worth failing a turn over.
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(0)
