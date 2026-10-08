"""Prove the /hooks endpoints work, since the panel now depends on them.

"修复 hook" used to spawn a second Python interpreter running
`python -m hud_daemon.settings_watch --once`. A packaged app has no Python, so
that path is gone; the button is an HTTP call to the running daemon now, and
this is the test that the call actually repairs rather than just answering 200.

    python tools\test_hooks_api.py [--port N]
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
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DAEMON_PKG = PROJECT_ROOT / "daemon"
PYTHON = sys.executable

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""),
          flush=True)


def request(port: int, method: str, path: str, timeout: float = 5.0):
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        sock.sendall(
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
        chunks = []
        while True:
            c = sock.recv(65536)
            if not c:
                break
            chunks.append(c)
    finally:
        sock.close()
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, json.loads(body.decode() or "{}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=17396)
    args = ap.parse_args()
    port = args.port

    with tempfile.TemporaryDirectory(prefix="cchud-hooks-") as tmp:
        env = dict(os.environ)
        env["APPDATA"] = tmp
        env["LOCALAPPDATA"] = tmp
        env["USERPROFILE"] = tmp
        env["HOME"] = tmp
        settings = Path(tmp) / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        # A realistic starting point: a user's config with no hooks of ours,
        # and one of their own that must survive every repair.
        settings.write_text(json.dumps({
            "model": "opus",
            "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": r'C:\mine\my-hook.exe'}]}]},
        }), encoding="utf-8")

        with open(Path(tmp) / "run.log", "wb") as logf:
            proc = subprocess.Popen(
                [PYTHON, "-m", "hud_daemon", "--port", str(port)],
                cwd=str(DAEMON_PKG), env=env,
                stdout=logf, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            up = False
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    status, body = request(port, "GET", "/status", timeout=2.0)
                    if status == 200:
                        up = True
                        break
                except Exception:
                    time.sleep(0.4)
            check("daemon came up", up)

            if not up:
                print((Path(tmp) / "run.log").read_text(
                    encoding="utf-8", errors="replace")[-2000:])
                return 1

            # 1 ── GET /hooks reports the injector's state
            status, body = request(port, "GET", "/hooks")
            check("GET /hooks reports the injector", status == 200 and body.get("enabled"),
                  f"enabled={body.get('enabled')} installed={body.get('installed')}")
            check("GET /hooks shows the command it installed",
                  bool(body.get("command")), str(body.get("command"))[:60])
            check("GET /hooks shows the watcher's own statistics",
                  "reinjected" in body, f"keys={sorted(body)[:6]}")

            # 2 ── the daemon installed its hooks on start
            check("daemon installed hooks into settings.json",
                  body.get("installed") is True)

            installed = json.loads(settings.read_text(encoding="utf-8"))
            check("user's own hook survived the install",
                  any("my-hook" in h.get("command", "")
                      for grp in installed.get("hooks", {}).values()
                      for entry in grp for h in entry.get("hooks", [])),
                  json.dumps(installed.get("hooks", {}))[:80])
            check("user's other settings untouched",
                  installed.get("model") == "opus", installed.get("model"))

            # 3 ── simulate cc-switch wiping our hooks, then repair
            without_ours = {"model": "opus", "hooks": {"PreToolUse": [
                {"matcher": "*", "hooks": [
                    {"type": "command", "command": r"C:\mine\my-hook.exe"}]}]}}
            settings.write_text(json.dumps(without_ours), encoding="utf-8")

            # Let the watcher notice on its own — that is the real recovery path,
            # and repair exists for when it is too slow or the user is impatient.
            restored = False
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                cfg = json.loads(settings.read_text(encoding="utf-8"))
                blob = json.dumps(cfg)
                if "cchud-hook" in blob:
                    restored = True
                    break
                time.sleep(0.25)
            check("watcher re-injected on its own after being wiped", restored)

            # 4 ── repair is idempotent and reports which happened
            # Wipe once more so this repair actually has work to do: the previous
            # repair found the hooks already present (the watcher had fixed it),
            # and a no-change repair correctly writes nothing — and so has no
            # backup to report. Asserting a backup there would be asserting
            # against the documented PatchResult semantics.
            settings.write_text(json.dumps(without_ours), encoding="utf-8")
            status, body = request(port, "POST", "/hooks/repair")
            check("POST /hooks/repair succeeds", status == 200 and body.get("ok"),
                  f"status={status} changed={body.get('changed')}")
            check("repair reports a backup path", bool(body.get("backup")),
                  str(body.get("backup"))[:50])
            check("a repair that had work to do reports changed=True",
                  body.get("changed") is True, f"changed={body.get('changed')}")

            status2, body2 = request(port, "POST", "/hooks/repair")
            check("a second repair is a no-op, not a rewrite",
                  status2 == 200 and body2.get("changed") is False,
                  f"changed={body2.get('changed')}")

            final = json.loads(settings.read_text(encoding="utf-8"))
            check("hooks still present after repair",
                  "cchud-hook" in json.dumps(final))
            check("user hook still present after repair",
                  any("my-hook" in h.get("command", "")
                      for grp in final.get("hooks", {}).values()
                      for entry in grp for h in entry.get("hooks", [])))
            check("model still untouched after repair",
                  final.get("model") == "opus")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()

    width = max(len(n) for n, _, _ in results)
    failed = [n for n, ok, _ in results if not ok]
    for name, ok, detail in results:
        if not ok:
            print(f"FAILED: {name.ljust(width)}  {detail}")
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
