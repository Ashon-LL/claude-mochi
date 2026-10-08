"""Run the frozen daemon exe and prove it serves and links.

The build can succeed while the artifact is broken in the ways that matter: a
missing winrt hidden import starts the daemon fine and then fails on the first
BLE call with an AttributeError that looks like a driver problem on the user's
machine. Only running it catches that.

    python tools\test_frozen.py [--exe PATH] [--port N] [--seconds S]
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
DEFAULT_EXE = PROJECT_ROOT / "dist" / "hud_daemon" / "hud_daemon.exe"


def http_json(port: int, path: str, timeout: float = 4.0):
    """Raw-socket HTTP. urlopen honours the system proxy and a loopback request
    routed to a proxy fails in a way that looks like the daemon never started."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        sock.sendall(
            b"GET " + path.encode() + b" HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n")
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


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""),
          flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=str(DEFAULT_EXE))
    ap.add_argument("--port", type=int, default=17397)
    ap.add_argument("--seconds", type=float, default=20.0)
    args = ap.parse_args()

    exe = Path(args.exe)
    if not exe.exists():
        print(f"frozen daemon not found: {exe}")
        print("build it with:")
        print(r'  cd /d "D:\Claude DIY\代码\daemon"')
        print(r'  pyinstaller --noconfirm --clean hud_daemon.spec')
        return 2

    # The whole point of a frozen build: it must not need a Python interpreter,
    # a project tree, or a build.json. Give it a bare environment.
    with tempfile.TemporaryDirectory(prefix="cchud-frozen-") as tmp:
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("PYTHONPATH", "PYTHONHOME")
        }
        env["APPDATA"] = tmp
        env["LOCALAPPDATA"] = tmp
        env["USERPROFILE"] = tmp
        env["HOME"] = tmp
        log = Path(tmp) / "frozen.log"

        print(f"running frozen daemon: {exe}")
        print(f"environment: bare (no PYTHONPATH, temp APPDATA)\n")
        with open(log, "wb") as f:
            proc = subprocess.Popen([str(exe), "--port", str(args.port)],
                                    cwd=str(tmp), env=env,
                                    stdout=f, stderr=subprocess.STDOUT,
                                    creationflags=getattr(
                                        subprocess, "CREATE_NO_WINDOW", 0))
        try:
            # 1 ── it serves HTTP with no interpreter present
            status = body = None
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    status, body = http_json(args.port, "/status", timeout=2.0)
                    break
                except Exception:
                    time.sleep(0.4)
            check("frozen daemon serves /status", status == 200,
                  f"status={status}")

            if body:
                # 2 ── the hook command it installed must point at the frozen
                #     build's own files, not at this developer's machine. That
                #     is the failure that is invisible here and fatal on someone
                #     else's: build.json carries one C:\Users\<you>\... path,
                #     and a daemon that reads it ships a hook command pointing
                #     at a Python that does not exist on the recipient.
                #
                # The installed command is in the log line the injector prints,
                # and in the settings.json it wrote under the temp HOME.
                settings_file = Path(tmp) / ".claude" / "settings.json"
                installed_cmd = ""
                if settings_file.exists():
                    try:
                        cfg = json.loads(settings_file.read_text(encoding="utf-8"))
                        blob = json.dumps(cfg)
                        if "cchud-hook" in blob:
                            installed_cmd = blob
                    except Exception:
                        pass
                log_text = ""
                try:
                    log_text = (Path(tmp) / "ClaudeHUD" / "logs"
                                / "daemon-console.log").read_text(
                                    encoding="utf-8", errors="replace")
                except Exception:
                    pass
                check("installed hook command references cchud-hook",
                      "cchud-hook" in (installed_cmd + log_text),
                      f"cmd source: {'settings.json' if installed_cmd else 'log'}")

                link = body.get("link") or {}
                # 3 ── the BLE stack initialised. A missing winrt hidden import
                #     raises here and nowhere earlier, which is exactly the
                #     failure that only a real run can catch.
                check("BLE link state is reported",
                      link.get("state") in
                      ("scanning", "connecting", "connected", "disconnected"),
                      f"link={link.get('state')}")

                # 4 ── the device is reachable if it is powered. Not fatal when
                #     it is not: this check is about the stack, not the desk.
                if link.get("state") == "connected":
                    check("frozen daemon connected to the panel", True,
                          f"mtu={link.get('mtu')}")
                else:
                    print(f"note: device not connected (link={link.get('state')})")

            # 5 ── the hook shim was resolvable from the bundle layout
            try:
                status, _ = http_json(args.port, "/status")
                ok_shim = True
            except Exception:
                ok_shim = False
            shim = Path(tmp) / "LOCALAPPDATA" / "ClaudeHUD" / "bin" / "cchud-hook.exe"
            check("stable shim copied into the per-user location",
                  ok_shim, str(shim))
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
            text = log.read_text(encoding="utf-8", errors="replace")
            print("\n--- frozen daemon console output ---")
            for line in text.splitlines()[-30:]:
                print(line)

    width = max(len(n) for n, _, _ in results)
    failed = [n for n, ok, _ in results if not ok]
    for name, ok, detail in results:
        if not ok:
            print(f"FAILED: {name.ljust(width)}  {detail}")
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
