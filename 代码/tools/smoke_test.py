"""smoke_test.py — drive the daemon end to end without a real device.

Verifies the parts that do not need hardware:
  * the daemon starts and binds its UDP listener
  * a hook datagram from the real shim reaches it
  * each of the five hook events maps to the right HUD state
  * the transient PostToolUse falls back to thinking after its hold
  * the shim exits 0 and does not block when no daemon is running

The BLE link will not find a device in most runs; that is fine, this test makes
no claim about the link.

    python tools/smoke_test.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAEMON_DIR = ROOT / "daemon"
SHIM = ROOT / "hookshim" / "cchud_hook.py"
PORT = 17321

# (hook event, state we expect in the log)
EXPECTED = [
    ("UserPromptSubmit", "thinking"),
    ("PreToolUse", "tool_start"),
    ("PostToolUse", "tool_end"),
    ("Stop", "idle"),
    ("Notification", "waiting"),
]

# Send order deliberately puts PostToolUse last. It is transient, and any event
# after it cancels the pending revert (that is the point of the state machine),
# so sending Stop or Notification afterwards would suppress the very fallback
# this test wants to observe.
SEND_ORDER = ["UserPromptSubmit", "PreToolUse", "Stop", "Notification", "PostToolUse"]

STARTUP_S = 4.0
# PostToolUse holds for tool_end_hold_ms (1s default) then reverts to thinking.
REVERT_WAIT_S = 2.5


def normalise(text: str) -> str:
    """Collapse whitespace so log padding does not break substring matching.

    The daemon pads hook event names to a fixed width, so a naive search for
    "PreToolUse -> tool_start" misses the spaces it is padded with.
    """
    return re.sub(r"\s+", " ", text)


def run() -> int:
    python = sys.executable
    log_path = Path(os.environ.get("TEMP", ".")) / "cchud_smoke.log"
    if log_path.exists():
        log_path.unlink()

    cmd = [python, "-m", "hud_daemon", "--no-http", "--debug"]
    print(f"[*] starting: {' '.join(cmd)}")
    with open(log_path, "w", encoding="utf-8") as logfile:
        proc = subprocess.Popen(cmd, cwd=DAEMON_DIR, stdout=logfile,
                                stderr=subprocess.STDOUT)

        failures: list[str] = []
        try:
            time.sleep(STARTUP_S)

            print("[*] sending hook events through the real shim")
            for event in SEND_ORDER:
                payload = json.dumps({"hook_event_name": event, "tool_name": "Bash",
                                      "session_id": "smoke"})
                result = subprocess.run(
                    [python, str(SHIM)], input=payload, capture_output=True, text=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    failures.append(f"shim exited {result.returncode} for {event}")
                time.sleep(0.6)

            print("[*] waiting for the transient revert")
            time.sleep(REVERT_WAIT_S)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    log = log_path.read_text(encoding="utf-8", errors="replace")
    print("\n".join("    " + ln for ln in log.splitlines()[-20:]))

    flat = normalise(log)
    print("\n[*] checks")

    if "ipc listening" not in flat:
        failures.append("daemon never reported its listener starting")

    for event, want in EXPECTED:
        needle = f"hook {event} -> {want} "
        if needle not in flat:
            failures.append(f"{event} did not map to {want}")

    # PostToolUse is transient, so a revert must follow without any new event.
    if "revert -> thinking" not in flat:
        failures.append("PostToolUse never reverted to thinking")

    # The shim must be silent and harmless with no daemon behind the port.
    result = subprocess.run([python, str(SHIM), "--test"], capture_output=True,
                            text=True, timeout=10)
    if result.returncode != 0:
        failures.append(f"shim --test exited {result.returncode} with no daemon")

    if failures:
        print("\n[FAIL]")
        for f in failures:
            print("  -", f)
        return 1
    print("[OK] hook path works end to end, including the transient revert")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
