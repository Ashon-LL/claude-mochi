"""test_hook_exe.py — verify the compiled cchud-hook.exe behaves like the Python one.

Checks the things that would otherwise only show up as "Claude Code ignores my
hook", each of which has happened before:

  * a normal event produces the right datagram
  * garbage stdin, empty stdin, and a non-object payload are all silently ignored
  * tool_input-sized payloads are truncated away, not forwarded
  * the exit code is 0 in every case, including the failure cases

    python tools/test_hook_exe.py
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "hookshim" / "cchud-hook-cs" / "bin" / "Release" / "net8.0" / "win-x64" / "publish" / "cchud-hook.exe"
PORT = 17321


def drain(sock: socket.socket) -> list[dict]:
    """Collect whatever datagrams arrived, decoding them."""
    out = []
    sock.settimeout(0.4)
    try:
        while True:
            data, _ = sock.recvfrom(4096)
            out.append(json.loads(data.decode("utf-8")))
    except socket.timeout:
        pass
    return out


def run_case(name: str, stdin_text: str, expect_event: str | None,
             expect_tool: str | None = None, expect_sid: str | None = None) -> bool:
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", PORT))

    try:
        started = time.perf_counter()
        result = subprocess.run([str(EXE)], input=stdin_text, capture_output=True,
                                text=True, timeout=10)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        got = drain(listener)
    finally:
        listener.close()

    problems: list[str] = []
    if result.returncode != 0:
        problems.append(f"exit code {result.returncode}")

    if expect_event is None:
        if got:
            problems.append(f"expected no datagram, got {got}")
    else:
        if not got:
            problems.append("no datagram arrived")
        else:
            pkt = got[0]
            if pkt.get("ev") != expect_event:
                problems.append(f"ev={pkt.get('ev')!r} want {expect_event!r}")
            if expect_tool is not None and pkt.get("tool") != expect_tool:
                problems.append(f"tool={pkt.get('tool')!r} want {expect_tool!r}")
            if expect_sid is not None and pkt.get("sid") != expect_sid:
                problems.append(f"sid={pkt.get('sid')!r} want {expect_sid!r}")
            # A large tool_input must never reach the wire.
            if pkt.get("tool_input") is not None:
                problems.append("tool_input was forwarded")

    status = "OK  " if not problems else "FAIL"
    print(f"[{status}] {name:<34} {elapsed_ms:6.1f} ms"
          + (f"  -> {'; '.join(problems)}" if problems else ""))
    return not problems


def main() -> int:
    if not EXE.exists():
        print(f"[FAIL] {EXE} not found — run dotnet publish first")
        return 1

    print(f"[*] testing {EXE.name}\n")

    big_input = "x" * 200_000   # simulates a tool_input carrying a whole file
    cases = [
        ("normal PreToolUse",
         json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                     "session_id": "s1"}),
         "PreToolUse", "Bash", "s1"),
        ("Stop event",
         json.dumps({"hook_event_name": "Stop", "session_id": "s2"}),
         "Stop", None, "s2"),
        ("empty stdin", "", None),
        ("not json", "this is not json at all", None),
        ("json but not an object", json.dumps([1, 2, 3]), None),
        ("object without hook_event_name",
         json.dumps({"tool_name": "Bash"}), None),
        ("null event name",
         json.dumps({"hook_event_name": None}), None),
        ("huge tool_input dropped",
         json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Read",
                     "tool_input": big_input}),
         "PreToolUse", "Read", None),
    ]

    ok = all(run_case(*c) for c in cases)

    # The shim must survive a hostile payload without hanging.
    print()
    print("[OK] compiled shim behaves as specified" if ok else "[FAIL] see above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
