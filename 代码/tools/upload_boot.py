"""Upload the start-up animation to the panel.

Speaks to the running daemon over HTTP rather than opening a BLE connection of
its own: the daemon owns the single central link, and a second central steals it
— both drop, and the symptom is a panel that goes dark with no error anywhere.

    python tools\\upload_boot.py [DIR] [--url URL] [--play]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent / "boot"
DEFAULT_URL = "http://127.0.0.1:17321"


def post(url: str, body: dict | None = None, timeout: float = 90.0) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        # The daemon's error body carries the device's verdict, which is the
        # only part worth showing: "device not connected" and "crc mismatch"
        # need different actions from the user.
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        except Exception:
            return exc.code, {}


def get(url: str, timeout: float = 8.0) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except urllib.error.URLError:
        print(f"cannot reach the daemon at {url}")
        print("is it running?  python -m hud_daemon")
        raise SystemExit(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("directory", nargs="?", default=str(DEFAULT_DIR))
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--play", action="store_true",
                    help="replay on the panel after uploading")
    args = ap.parse_args()

    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"not a directory: {directory}")
        print("\ngenerate one with:")
        print(r'  python tools\mochi_to_boot.py '
              r'"F:\桌宠代码\clawd-mochi\clawd_mochi\clawd_mochi.ino" '
              r'代码\tools\boot')
        return 2

    for required in ("meta.json", "segs.bin", "tris.bin"):
        if not (directory / required).exists():
            print(f"missing {required} in {directory}")
            return 2

    status, targets = get(f"{args.url}/boot")
    if status != 200:
        print("daemon did not answer /boot")
        return 1

    print(f"uploading from {directory}")
    for t in targets.get("targets", []):
        print(f"  {t['name']}")

    status, body = post(f"{args.url}/boot/upload", {"dir": str(directory)})
    if status != 200 or not body.get("ok"):
        print(f"upload failed: {body.get('error', status)}")
        for f in body.get("files", []):
            if f.get("error"):
                print(f"  {f['name']}: {f['error']}")
        return 1

    print(f"uploaded {body.get('bytes')} bytes:")
    for f in body.get("files", []):
        print(f"  {f['name']:12} {f['bytes']:>6} bytes")

    if args.play:
        status, body = post(f"{args.url}/boot/play")
        if status == 200:
            print("replaying on the panel")
        else:
            print(f"replay failed: {body.get('error', status)}")
    else:
        print("\nthe panel reloads these at boot; restart it to see the animation,")
        print("or re-run with --play to watch it without rebooting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
