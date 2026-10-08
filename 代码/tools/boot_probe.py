"""Start the daemon with a throwaway APPDATA/HOME and capture its output.

Used when the daemon fails to come up: the console log carries the import and
start-up errors that /status can never show.

    python tools\boot_probe.py [--port N] [--seconds S]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAEMON_PKG = PROJECT_ROOT / "daemon"
PYTHON = sys.executable


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=17398)
    ap.add_argument("--seconds", type=float, default=12.0)
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="cchud-boot-") as tmp:
        env = dict(os.environ)
        env["APPDATA"] = tmp
        env["USERPROFILE"] = tmp
        env["HOME"] = tmp

        out = Path(tmp) / "run.log"
        print(f"boot probe: python={PYTHON}")
        print(f"cwd={DAEMON_PKG}")
        print(f"log={out}\n")

        with open(out, "wb") as f:
            proc = subprocess.Popen(
                [PYTHON, "-m", "hud_daemon", "--debug", "--port", str(args.port)],
                cwd=str(DAEMON_PKG), env=env,
                stdout=f, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                proc.wait(timeout=args.seconds)
                print(f"daemon exited on its own with rc={proc.returncode}")
                print("(that is the failure: it should stay up)")
            except subprocess.TimeoutExpired:
                print(f"daemon is still running after {args.seconds}s (good)")
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()

        text = out.read_text(encoding="utf-8", errors="replace")
        print("\n--- daemon console output ---")
        print(text if text.strip() else "(empty — the process printed nothing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
