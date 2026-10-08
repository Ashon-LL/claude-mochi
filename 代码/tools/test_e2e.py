"""End-to-end run of the real daemon, with the feedback-loop fixes in place.

The unit tests in test_feedback.py cover the pieces. This covers the wiring
between them, which is where a NameError or a start-up ordering bug actually
lives: the daemon is started as a separate process, then driven over the same
two transports the front half uses.

Checks, in order:

  1. the daemon starts and serves /status
  2. the hook path works over UDP -- the EVENT_FIELDS regression. Before the
     fix, the first event killed the worker task and events_seen stayed at 0
     forever, with nothing in /status to show it.
  3. the hook path works over HTTP
  4. a WebSocket connects and receives pushed frames
  5. the pushed frames include the types the UI now depends on

Everything runs against a throwaway APPDATA and USERPROFILE, so the real
~/.claude/settings.json and %APPDATA%\\ClaudeHUD are never touched.

    python tools/test_e2e.py [--port N]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent            # ...\代码\tools -> ...\代码
DAEMON_PKG = PROJECT_ROOT / "daemon"

# There are two daemons on this machine with the same module name, and picking
# the wrong one is silent: `python -m hud_daemon` with the wrong cwd runs the
# old D:\Claude DIY\daemon\hud_daemon.py, which has no HTTP server at all — so
# /status is refused and the BLE device gets fought over. Building the path the
# obvious way (parent, then parents[1]) lands on D:\Claude DIY instead of 代码.
# Boot the daemon from an explicit cwd so there is no ambiguity.
assert DAEMON_PKG.is_dir(), f"daemon package dir not found: {DAEMON_PKG}"
assert (DAEMON_PKG / "hud_daemon" / "__main__.py").exists(), (
    f"{DAEMON_PKG} is not the package daemon; expected "
    f"{DAEMON_PKG / 'hud_daemon' / '__main__.py'}")

PYTHON = sys.executable
if not PYTHON or "WindowsApps" in PYTHON:
    # The Store stub can start nothing real.
    for candidate in (
        r"C:\Users\wyy12\AppData\Local\Programs\Python\Python312\python.exe",
        r"C:\Users\wyy12\AppData\Local\Programs\Python\Python313\python.exe",
    ):
        if Path(candidate).exists():
            PYTHON = candidate
            break

STARTUP_TIMEOUT_S = 25.0
WS_WAIT_S = 8.0          # a status broadcast arrives on the heartbeat interval
HTTP_WAIT_S = 6.0

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}"
          + (f"  {detail}" if detail else ""), flush=True)


def http_json(port: int, path: str, method: str = "GET",
              body: dict | None = None, timeout: float = 5.0):
    """A minimal HTTP/1.1 client over a raw socket.

    Deliberately not urllib: urlopen honours the system proxy settings, and a
    loopback request that gets routed to a proxy fails in a way that looks
    exactly like "the daemon never started". That cost a full debugging cycle
    before it was traced back here.
    """
    payload = json.dumps(body).encode() if body is not None else b""
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        req = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Connection: close\r\n"
            + (f"Content-Length: {len(payload)}\r\n"
               "Content-Type: application/json\r\n" if payload else "")
            + "\r\n"
        ).encode() + payload
        sock.sendall(req)

        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        sock.close()

    head, _, resp = raw.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    # uvicorn sets Content-Length; no chunked encoding on these plain replies.
    parsed = json.loads(resp.decode("utf-8")) if resp.strip() else {}
    return status, parsed, {}


def ws_read_frames(port: int, seconds: float) -> list[dict]:
    """Collect everything the daemon pushes for `seconds`. No dependency on a
    WebSocket client library: the frames are one JSON object per message and the
    server never sends fragmented control frames, so a raw socket is enough."""
    import base64
    import hashlib
    import struct

    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
    try:
        sock.sendall(
            b"GET /ws HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Key: " + key.encode() + b"\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n")
        # Read the handshake reply, header-terminated.
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return []
            buf += chunk
        if b"101" not in buf.split(b"\r\n", 1)[0]:
            return []

        frames: list[dict] = []
        deadline = time.monotonic() + seconds
        sock.settimeout(1.0)
        while time.monotonic() < deadline:
            try:
                header = sock.recv(2)
            except socket.timeout:
                continue
            if len(header) < 2:
                break
            opcode = header[0] & 0x0F
            ln = header[1] & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", sock.recv(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", sock.recv(8))[0]
            payload = b""
            while len(payload) < ln:
                payload += sock.recv(ln - len(payload))
            if opcode != 1:      # 1 = text
                continue
            try:
                frames.append(json.loads(payload.decode("utf-8", "replace")))
            except ValueError:
                pass
        return frames
    finally:
        sock.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=17399)
    args = ap.parse_args()
    port = args.port

    with tempfile.TemporaryDirectory(prefix="cchud-e2e-") as tmp:
        env = dict(os.environ)
        env["APPDATA"] = tmp
        env["USERPROFILE"] = tmp          # keeps ~/.claude/settings.json pristine
        env["HOME"] = tmp
        log_path = Path(tmp) / "daemon-run.log"

        print(f"starting daemon on 127.0.0.1:{port} (python {PYTHON})")
        with open(log_path, "wb") as logf:
            proc = subprocess.Popen(
                [PYTHON, "-m", "hud_daemon", "--debug", "--port", str(port)],
                cwd=str(DAEMON_PKG), env=env,
                stdout=logf, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        try:
            # 1 ── it serves /status at all
            status = status_body = None
            deadline = time.monotonic() + STARTUP_TIMEOUT_S
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    check("daemon stayed up", False,
                          f"exited rc={proc.returncode}")
                    break
                try:
                    status, status_body, _ = http_json(port, "/status", timeout=2.0)
                    break
                except Exception:
                    time.sleep(0.4)
            if status is None:
                check("daemon served /status", False, "no response")
            else:
                check("daemon served /status", status == 200, f"body keys={sorted(status_body)}")

            if status_body is not None:
                before = status_body.get("events_seen", 0)

                # 2 ── the UDP path, exactly as the shim sends it
                # {"v":1,"ev":"PreToolUse","tool":"Bash","sid":"...","src":"cchud-hook"}
                shim = json.dumps(
                    {"v": 1, "ev": "PreToolUse", "tool": "Bash",
                     "sid": "e2e", "src": "cchud-hook"},
                    separators=(",", ":")).encode()
                udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                udp.sendto(shim, ("127.0.0.1", port))
                udp.close()

                seen = before
                deadline = time.monotonic() + HTTP_WAIT_S
                while time.monotonic() < deadline:
                    try:
                        _, body, _ = http_json(port, "/status", timeout=2.0)
                        if body.get("events_seen", 0) > before:
                            seen = body["events_seen"]
                            break
                    except Exception:
                        pass
                    time.sleep(0.3)

                # events_seen increments, and -- decisively -- dropped stayed 0.
                # Before the fix, the worker died and udp_packets climbed while
                # events_seen never moved.
                try:
                    _, body, _ = http_json(port, "/status", timeout=2.0)
                    dropped = body.get("dropped", 0)
                except Exception:
                    dropped = -1

                check("UDP hook event reached the worker",
                      seen > before, f"events_seen {before} -> {seen}")
                check("worker did not drop the event", dropped == 0,
                      f"dropped={dropped}")

                # 3 ── the HTTP path, which carries Claude Code's own field name
                hstatus, _, _ = http_json(
                    port, "/hook", method="POST",
                    body={"session_id": "e2e", "hook_event_name": "UserPromptSubmit",
                          "tool_name": "Bash"})
                check("POST /hook accepted", hstatus == 200, f"status={hstatus}")

                try:
                    _, body, _ = http_json(port, "/status", timeout=2.0)
                    check("HTTP hook event counted too",
                          body.get("events_seen", 0) > seen,
                          f"events_seen -> {body.get('events_seen')}")
                except Exception as exc:
                    check("HTTP hook event counted too", False, str(exc))

            # 4 ── the push channel delivers frames.
            # Guarded: a crash here must not hide the daemon's log, which is the
            # only place a start-up failure explains itself.
            try:
                frames = ws_read_frames(port, WS_WAIT_S)
            except Exception as exc:
                frames = []
                check("WebSocket connected", False, f"{type(exc).__name__}: {exc}")
            else:
                types = sorted({str(f.get("type")) for f in frames})
                check("WebSocket received pushed frames", bool(frames),
                      f"{len(frames)} frames, types={types}")
                check("pushed frames include a status broadcast",
                      "status" in types, f"types={types}")

            # 5 ── and the daemon is still alive afterwards
            check("daemon still running after the exercise",
                  proc.poll() is None, f"rc={proc.poll()}")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
            # The daemon's own log, always. An early crash shows up here and
            # nowhere else, and the exception paths above all skip past the
            # summary at the bottom.
            tail = log_path.read_text(encoding="utf-8", errors="replace")
            if tail.strip():
                print("\n--- daemon console output ---")
                for line in tail.splitlines()[-25:]:
                    print(line)
            else:
                print("\n--- daemon console output was EMPTY ---")

        # Show the tail of the daemon's own log when anything failed: the run's
        # log is the only place a start-up ordering bug shows up.
        failed = [name for name, ok, _ in results if not ok]
        if failed:
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            print("\n--- daemon log tail (last 25 lines) ---")
            for line in tail[-25:]:
                print(line)

    width = max(len(n) for n, _, _ in results)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        if not ok:
            print(f"FAILED: {name.ljust(width)}  {detail}")
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
