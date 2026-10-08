"""Boot a real daemon, upload the animation, and report what the panel said.

The unit test covers the packing and the budget rules; this covers the whole
path including the part that has no test double: two-and-a-half kilobytes of
binary arriving at a device that streams it back from LittleFS and reports what
it found. That is the step where a format misunderstanding shows up as a garbled
logo, and it is invisible from the host side alone.

Writes to the real device, so it is not something to run casually.

    python tools\\test_boot_upload.py [--dir DIR] [--port N]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DAEMON_PKG = PROJECT_ROOT / "daemon"
PYTHON = sys.executable


def http(port: int, method: str, path: str, body: dict | None = None,
         timeout: float = 60.0):
    """Raw-socket HTTP, because urlopen honours the system proxy and a loopback
    request routed there fails in a way that looks like the daemon never ran."""
    import socket

    payload = json.dumps(body).encode() if body is not None else b""
    req = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Connection: close\r\n"
        + (f"Content-Length: {len(payload)}\r\n"
           "Content-Type: application/json\r\n" if payload else "")
        + "\r\n"
    ).encode() + payload

    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        sock.sendall(req)
        chunks = []
        while True:
            c = sock.recv(65536)
            if not c:
                break
            chunks.append(c)
    finally:
        sock.close()
    raw = b"".join(chunks)
    head, _, resp = raw.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    # A non-JSON body is itself information: an empty reply means the daemon
    # hung or the socket closed early, which is a different problem from an
    # error the daemon chose to report. Print it rather than raising.
    if not resp.strip():
        return status, {"_raw": head.decode("utf-8", "replace")}
    try:
        return status, json.loads(resp.decode())
    except ValueError:
        return status, {"_raw": resp[:400].decode("utf-8", "replace")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(PROJECT_ROOT / "tools" / "boot"))
    ap.add_argument("--port", type=int, default=17321)
    args = ap.parse_args()

    bootdir = Path(args.dir)
    missing = [n for n in ("meta.json", "segs.bin", "tris.bin")
               if not (bootdir / n).exists()]
    if missing:
        print(f"missing in {bootdir}: {', '.join(missing)}")
        print("generate with: python tools\\mochi_to_boot.py <mochi.ino> "
              f"{bootdir}")
        return 2

    total = sum((bootdir / n).stat().st_size
                for n in ("meta.json", "segs.bin", "tris.bin"))
    print(f"boot files in {bootdir}")
    print(f"  meta.json  {(bootdir / 'meta.json').stat().st_size:>6} bytes")
    print(f"  segs.bin   {(bootdir / 'segs.bin').stat().st_size:>6} bytes")
    print(f"  tris.bin   {(bootdir / 'tris.bin').stat().st_size:>6} bytes")
    print(f"  total      {total:>6} bytes\n")

    port = args.port
    with tempfile.TemporaryDirectory(prefix="cchud-boot-") as tmp:
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["APPDATA"] = tmp
        env["LOCALAPPDATA"] = tmp
        env["USERPROFILE"] = tmp
        env["HOME"] = tmp
        log_path = Path(tmp) / "run.log"

        with open(log_path, "wb") as f:
            proc = subprocess.Popen(
                [PYTHON, "-m", "hud_daemon", "--port", str(port)],
                cwd=str(DAEMON_PKG), env=env,
                stdout=f, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            up = False
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    status, _ = http(port, "GET", "/status", timeout=2.0)
                    if status == 200:
                        up = True
                        break
                except Exception:
                    time.sleep(0.4)
            if not up:
                print("daemon did not start")
                return 1

            # Let the link settle: uploading while it is mid-reconnect is the
            # one case where a failure means nothing about the format.
            print("waiting for the BLE link…")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    _, body = http(port, "GET", "/status", timeout=2.0)
                    if (body.get("link") or {}).get("state") == "connected":
                        print(f"  connected, mtu={body['link'].get('mtu')}\n")
                        break
                except Exception:
                    pass
                time.sleep(0.5)

            print(f"uploading from {bootdir} …")
            started = time.monotonic()
            status, body = http(port, "POST", "/boot/upload", {"dir": str(bootdir)})
            elapsed = time.monotonic() - started

            if status != 200 or not body.get("ok"):
                print(f"UPLOAD FAILED after {elapsed:.1f}s: "
                      f"{body.get('error', status)}")
                for f in body.get("files", []):
                    if f.get("error"):
                        print(f"  {f['name']}: {f['error']}")
                # A 500 is a daemon-side crash, and its traceback is in the
                # daemon's log — the only place it exists.
                text = log_path.read_text(encoding="utf-8", errors="replace")
                tail = text.splitlines()[-30:]
                if any("Traceback" in ln or "Error" in ln for ln in tail):
                    print("\n--- daemon log tail ---")
                    for ln in tail:
                        print(f"  {ln}")
                return 1

            print(f"uploaded {body.get('bytes')} bytes in {elapsed:.1f}s "
                  f"({body.get('bytes', 0) / max(elapsed, 0.01) / 1024:.0f} KB/s)")
            for f in body.get("files", []):
                print(f"  {f['name']:12} {f['bytes']:>6} bytes")

            print("\nreplaying on the panel…")
            status, body = http(port, "POST", "/boot/play", timeout=20.0)
            if status == 200:
                print("  the panel is playing the animation now")
            else:
                print(f"  replay failed: {body.get('error', status)}")

            # What the device said is the only verdict that matters.
            text = log_path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if "boot:" in line:
                    print(f"  device: {line.split('device: ')[-1]}")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()

    print("\ndone. If the panel showed the logo, the whole path works.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
