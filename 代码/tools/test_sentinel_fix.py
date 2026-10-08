"""Verify the frozen daemon ignores a stale stop sentinel.

Written after finding the bug that made the HUD permanently dead: the app wrote
a stop request while no daemon was running, nothing consumed it, and every
daemon started after that exited on start-up. Running the actual frozen binary
is the only way to prove the shipped artifact behaves, because the fix lives in
the code that gets compiled in.

    python tools\\test_sentinel_fix.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

DAEMON = Path(r"D:\Claude DIY\代码\dist\hud_daemon\hud_daemon.exe")
PORT = 17388


def main() -> int:
    if not DAEMON.exists():
        print(f"frozen daemon not found: {DAEMON}")
        print("build it: pyinstaller hud_daemon.spec")
        return 2

    base = Path(os.environ["LOCALAPPDATA"]) / "ClaudeHUD"
    base.mkdir(parents=True, exist_ok=True)
    sentinel = base / "stop"

    # Clear anything left over, so the test measures what it sets up.
    if sentinel.exists():
        sentinel.unlink()

    # A one-hour-old request: long past any legitimate "please stop now".
    sentinel.write_text("stale", encoding="utf-8")
    old = time.time() - 3600
    os.utime(sentinel, (old, old))
    print(f"planted a 1-hour-old stop request at {sentinel}")

    proc = subprocess.Popen(
        [str(DAEMON), "--port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        time.sleep(5)
        alive = proc.poll() is None
        print(f"daemon alive after 5s: {alive}  (the brick bug would make this False)")
        print(f"stale request cleared: {not sentinel.exists()}")

        # And it must still be serving, not merely lingering.
        import json
        import socket
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=3)
            s.sendall(b"GET /status HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      b"Connection: close\r\n\r\n")
            buf = b""
            while True:
                c = s.recv(4096)
                if not c:
                    break
                buf += c
            s.close()
            serving = b"200" in buf.split(b"\r\n", 1)[0]
        except OSError as exc:
            serving = False
            print(f"  (status probe failed: {exc})")
        print(f"daemon serving /status: {serving}")

        out = proc.stdout.read1(65536).decode("utf-8", "replace") \
            if hasattr(proc.stdout, "read1") else ""
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()

    ok = alive and not sentinel.exists() and serving
    print("\n" + ("PASS" if ok else "FAIL")
          + ": a stale stop request can no longer brick the daemon")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
