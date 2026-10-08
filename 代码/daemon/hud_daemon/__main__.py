"""__main__.py — daemon entry point.

    python -m hud_daemon [--debug] [--cycle] [--port N] [--no-http]

Starts three things together:

  * the BLE link, which owns the device connection
  * the UDP hook listener on 127.0.0.1:PORT, which is what Claude Code's hook
    shim actually talks to
  * the HTTP/WebSocket API on the same port, for the Electron UI

Everything printed — including uvicorn's ASGI tracebacks, which bypass the
logging framework and write straight to stderr — is teed to a log file, because
an exception that only ever appears on a console the user has already scrolled
past is an exception that never gets reported.

--cycle is a diagnostic: it drives all seven faces on a timer so the panel can
be verified without Claude Code running. Off by default, because with the hook
path working it would fight the real state machine.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import uvicorn

from . import protocol as P
from .ble_link import BleLink
from .config import DEVICE_NAME, RX_UUID, SERVICE_UUID, TX_UUID, Settings
from .device import DeviceConfig
from .expressions import config as send_device_config
from .ipc_server import IpcServer
from .logbus import app_dir, configure, log
from .state_map import StateMapper

CYCLE_HOLD_S = 3.0
LOG_NAME = "daemon-console.log"

# The UI's "退出" writes this file to ask the daemon to go away.
#
# It has to be a file rather than a signal, because the daemon is no longer a
# child of the UI: it is detached and started at logon, so a detached process is
# the one relationship Node cannot reach across. A file is visible to both, and
# its absence is the normal case, so a false stop is as impossible as a missed
# one is unlikely.
#
# Path must not depend on the app's install directory — after an uninstall there
# is no app left, and a daemon that cannot find its own data directory cannot
# stop. This mirrors the Electron side's stopSentinel().
STOP_SENTINEL_REL = "stop"


def _stop_sentinel_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") \
        or os.path.expanduser("~")
    return Path(base) / "ClaudeHUD" / STOP_SENTINEL_REL


# How long a stop request stays meaningful. An older file is ignored, because it
# was written by a daemon that has since exited, or by an app that quit while no
# daemon was running — in both cases the request was already served, and acting
# on it again would make the next daemon exit on start-up. That is the failure
# this guards against: a HUD that looks permanently dead for a reason no log
# mentions.
STOP_SENTINEL_MAX_AGE_S = 60.0


def consume_stop_request() -> bool:
    """Return True when a stop has been requested, and clear it.

    The file is removed rather than left in place: a sentinel that survives its
    own daemon means the next one exits on start-up, which is a very confusing
    way to discover that you once pressed 退出.
    """
    path = _stop_sentinel_path()
    try:
        if not path.exists():
            return False
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0.0
        # Always clear it, fresh or not: leaving a stale one behind would brick
        # every start until someone found this file by hand.
        path.unlink()
        if age > STOP_SENTINEL_MAX_AGE_S:
            log.info("ignoring a stale stop request (%.0fs old)", age)
            return False
        log.info("stop sentinel found; shutting down")
        return True
    except OSError as exc:
        log.warning("could not read the stop sentinel: %s", exc)
        return False


class _Tee:
    """Mirror a stream to a file as well as the console.

    Uvicorn writes its ASGI exception tracebacks directly to stderr, so they
    never reach the "cchud" logger and never reach the rotating daemon.log.
    Intercepting the stream itself is the only way to capture them, and it also
    captures anything a dependency prints on a whim.
    """

    def __init__(self, stream, path: Path) -> None:
        self._stream = stream
        self._file = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, data: str) -> int:
        written = self._stream.write(data)
        self._file.write(data)
        return written

    def flush(self) -> None:
        self._stream.flush()
        self._file.flush()

    def isatty(self) -> bool:
        return getattr(self._stream, "isatty", lambda: False)()

    def fileno(self) -> int:
        return self._stream.fileno()


def install_console_tee() -> Path:
    """Route stdout/stderr through _Tee. Returns the log file's path."""
    path = app_dir() / "logs" / LOG_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = _Tee(sys.__stdout__, path)
        sys.stderr = _Tee(sys.__stderr__, path)
    except OSError:
        # Losing the mirror must not stop the daemon; the console still works.
        print("[main] could not open console log", file=sys.__stderr__)
    return path


async def heartbeat(link: BleLink, ipc: IpcServer, settings: Settings) -> None:
    """Ping forever.

    The firmware drops to OFFLINE after 30s of silence, so a missing heartbeat
    is visible on the panel as well as in the log — which is the point: it makes
    a dead daemon look dead instead of showing a stale face.
    """
    config_pushed = False
    while True:
        await asyncio.sleep(settings.heartbeat_s)
        if link.connected:
            # Push the display settings once per connection. A reflashed device
            # has an empty NVS, and without this the panel would sit at default
            # brightness while the UI showed whatever the host last remembered —
            # two different truths, with no way to tell them apart.
            if not config_pushed:
                cfg = ipc.device_cfg
                await send_device_config(link, cfg.brightness, cfg.speed,
                                         cfg.rotation, cfg.idle_s)
                config_pushed = True
                log.info("device config pushed (brightness=%d speed=%d rot=%d idle=%ds)",
                         cfg.brightness, cfg.speed, cfg.rotation, cfg.idle_s)
            await link.ping()
        else:
            config_pushed = False
        await ipc._broadcast({"type": "status", **ipc._status_dict()})


async def watch_stop_sentinel(interval_s: float = 1.0) -> None:
    """Exit the daemon when the UI asks it to.

    Separate from the heartbeat, and with its own 1s poll rather than the
    heartbeat's 5s, for two reasons: a user who just pressed 退出 should not
    wait five seconds to see it happen, and the heartbeat is otherwise a job the
    stop check would be coupled to for no reason.

    Returns when the sentinel is consumed, which the caller treats as "stop".
    """
    while not consume_stop_request():
        await asyncio.sleep(interval_s)


async def cycle_faces(link: BleLink) -> None:
    """Diagnostic only: step through every state so all faces can be eyeballed."""
    order = [P.ST_IDLE, P.ST_THINKING, P.ST_TOOL_START, P.ST_TOOL_END,
             P.ST_WAITING, P.ST_ERROR, P.ST_OFFLINE]
    tick = 0
    while True:
        await asyncio.sleep(CYCLE_HOLD_S)
        if not link.connected:
            continue
        await link.send(P.MSG_STATE, bytes((order[tick % len(order)],)))
        tick += 1


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hud_daemon")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--cycle", action="store_true",
                        help="cycle all faces on a timer (diagnostic)")
    parser.add_argument("--no-http", action="store_true",
                        help="UDP hook listener only, no HTTP/WS server")
    parser.add_argument("--shim", choices=("exe", "script"), default="exe",
                        help="which hook shim the settings.json command installs "
                             "(see paths.py and build.json)")
    parser.add_argument("--port", type=int, default=0,
                        help="override the port from config.json")
    args = parser.parse_args(argv)

    configure(level=logging.DEBUG if args.debug else logging.INFO)

    # Tee before anything else can print, so even an import-time failure lands
    # in the file. ConsoleHandler is added after so its output flows through.
    console_log = install_console_tee()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                           datefmt="%H:%M:%S"))
    log.addHandler(console)

    settings = Settings.load()
    if args.port:
        settings.port = args.port

    log.info("daemon starting (device=%s port=%d)", DEVICE_NAME, settings.port)
    log.info("console mirror: %s", console_log)

    link = BleLink(
        service_uuid=SERVICE_UUID,
        rx_uuid=RX_UUID,
        tx_uuid=TX_UUID,
    )
    mapper = StateMapper(dedupe_ms=settings.dedupe_ms,
                         tool_end_hold_ms=settings.tool_end_hold_ms)
    ipc = IpcServer(link=link, settings=settings, mapper=mapper)

    # The device frames are routed through the IPC server, which is the only
    # thing that can both log them and push them to the UI. BleLink takes its
    # handlers at construction but is built before ipc exists (it needs the link
    # to send through), so they are attached a line later. Nothing can arrive in
    # between: ble_link.start() has not been called yet.
    ipc.bind_device_handlers(link)

    # The hook injector belongs to the daemon, not to a manual step. Leaving it
    # out made "start on boot" a lie: the daemon came up, the panel showed a
    # healthy link, and nothing drove the HUD because no hook was ever
    # installed — with nothing anywhere saying so.
    watcher = None
    if settings.hooks_enabled:
        from .paths import hookshim_command
        from .settings_patch import SettingsPatcher
        from .settings_watch import SettingsWatcher

        patcher = SettingsPatcher(Path.home() / ".claude" / "settings.json",
                                  hookshim_command(args.shim),
                                  enabled=settings.hooks_enabled)
        watcher = SettingsWatcher(patcher)
        watcher.apply_once()
        ipc.watcher = watcher

    tasks: list[asyncio.Task] = []
    try:
        await link.start()
        await ipc.start()

        if not args.no_http:
            config = uvicorn.Config(ipc.app, host="127.0.0.1", port=settings.port,
                                    log_level="warning", access_log=False)
            server = uvicorn.Server(config)
            tasks.append(asyncio.create_task(server.serve(), name="http"))

        tasks.append(asyncio.create_task(heartbeat(link, ipc, settings), name="heartbeat"))
        if watcher is not None:
            tasks.append(asyncio.create_task(watcher.run(), name="settings-watch"))
        if args.cycle:
            tasks.append(asyncio.create_task(cycle_faces(link), name="cycle"))

        # The stop sentinel is awaited on its own because it is the only task
        # meant to finish normally: it returns when the UI asks the daemon to
        # exit. Everything else runs until it does, then gets cancelled so the
        # finally block can close the BLE link and the UDP listener cleanly.
        #
        # Waiting on FIRST_COMPLETED over both sets — rather than gather()ing
        # the work tasks alone — is what lets a stop take effect while the BLE
        # link is in the middle of a long reconnect backoff.
        stop_task = asyncio.create_task(watch_stop_sentinel(), name="stop-watch")
        try:
            done, _ = await asyncio.wait([stop_task, *tasks],
                                         return_when=asyncio.FIRST_COMPLETED)
            # Only claim a stop when the sentinel is what finished. Any other
            # task ending here means it died, and calling that a stop would put
            # a lie in the log at exactly the moment someone reads it to find
            # out what happened.
            if stop_task in done:
                log.info("stop requested via sentinel")
            else:
                for t in tasks:
                    if t.done() and t.exception() is not None:
                        log.error("task %s died: %s", t.get_name(), t.exception())
        finally:
            for t in [stop_task, *tasks]:
                if not t.done():
                    t.cancel()
            await asyncio.gather(stop_task, *tasks, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        await ipc.stop()
        await link.stop()
        log.info("daemon stopped")
    return 0


def _selftest() -> None:
    """The stop sentinel, in isolation.

    Both directions matter: a file that survives its daemon makes the next one
    exit on start-up, and a file that is never read makes 退出 a no-op that
    leaves a detached process holding the BLE link.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LOCALAPPDATA"] = tmp
        os.environ["APPDATA"] = tmp

        sentinel = _stop_sentinel_path()
        assert sentinel.parent.name == "ClaudeHUD", sentinel

        # Absent by default: a daemon must never invent a stop for itself.
        assert consume_stop_request() is False

    # Present: consumed exactly once, and removed.
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("123", encoding="utf-8")
    assert consume_stop_request() is True
    assert not sentinel.exists(), "the sentinel must not outlive its daemon"
    assert consume_stop_request() is False

    # A stale request must be ignored AND cleared.
    #
    # This is the brick scenario: the app quits while no daemon is running, the
    # file is written and nothing consumes it, and every daemon started after
    # that exits on start-up — a HUD that looks permanently dead. Ignoring the
    # age alone would leave the file there to bite the next start too, so both
    # halves are asserted.
    import os as _os
    sentinel.write_text("123", encoding="utf-8")
    old = time.time() - STOP_SENTINEL_MAX_AGE_S - 30
    _os.utime(sentinel, (old, old))
    assert consume_stop_request() is False, "a stale request must not stop us"
    assert not sentinel.exists(), "a stale request must still be cleared"

    # The actual exit path, not just the flag.
    async def _run() -> None:
        sentinel.write_text("123", encoding="utf-8")
        task = asyncio.create_task(watch_stop_sentinel(interval_s=0.01))
        await asyncio.wait_for(task, timeout=2.0)
    asyncio.run(_run())
    assert not sentinel.exists(), "watch_stop_sentinel must clear the file"

    # A fresh request the daemon has already served must not fire twice.
    sentinel.write_text("123", encoding="utf-8")
    assert consume_stop_request() is True
    assert consume_stop_request() is False, "consumed once, not every poll"

    print("__main__ selftest OK")


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
