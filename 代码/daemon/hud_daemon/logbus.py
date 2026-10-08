"""logbus.py — one logging setup for the whole daemon.

Two sinks, because they serve different readers:

  * a rotating file, so a crash an hour from now can still be diagnosed after
    the daemon has been restarted a dozen times
  * an in-memory ring buffer, so the UI's log view and the CLI tail show what
    just happened without touching the disk

The ring buffer carries a monotonically increasing version so the WebSocket
stream can send deltas instead of the whole tail on every new line.

Import `log` from here rather than creating your own logger: one named logger
means one place to change the level, and the UI's filter keys off the name.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import threading
from collections import deque
from pathlib import Path

APP_DIR_NAME = "ClaudeHUD"

log = logging.getLogger("cchud")


def app_dir() -> Path:
    """Per-user writable directory, created on first use.

    APPDATA on Windows, ~/.config-like fallback elsewhere. Deliberately not the
    install directory: a daemon started from Program Files cannot write there.
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = Path(base) / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


class RingHandler(logging.Handler):
    """Keeps the most recent records in memory for the log view."""

    def __init__(self, capacity: int = 1000) -> None:
        super().__init__()
        self._buf: deque[str] = deque(maxlen=capacity)
        self._version = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._lock:
            self._buf.append(line)
            self._version += 1

    def snapshot(self) -> tuple[list[str], int]:
        """Return (lines, version). Callers keep the version and pass it back
        later to tell whether anything new arrived."""
        with self._lock:
            return list(self._buf), self._version

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


_ring: RingHandler | None = None
_configure_lock = threading.Lock()


def configure(*, level: int = logging.INFO, max_bytes: int = 1 << 20, backup_count: int = 3) -> RingHandler:
    """Install the ring and file handlers on the "cchud" logger.

    Idempotent: a second call only changes the level, so a CLI entry point can
    raise verbosity without duplicating handlers or double-writing the file.
    """
    global _ring
    with _configure_lock:
        if _ring is None:
            _ring = RingHandler()
            _ring.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                                 datefmt="%H:%M:%S"))

            log.setLevel(level)
            log.propagate = False   # never leak into the root logger's handlers

            log_file = app_dir() / "logs" / "daemon.log"
            try:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                file_handler = logging.handlers.RotatingFileHandler(
                    log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
                )
                file_handler.setFormatter(logging.Formatter(
                    "%(asctime)s %(levelname)-7s %(name)s %(message)s"))
                log.addHandler(file_handler)
            except OSError as exc:
                # Losing the file log is survivable; losing the daemon is not.
                print(f"[logbus] file log unavailable: {exc}", file=sys.stderr)

            log.addHandler(_ring)
        else:
            log.setLevel(level)
        return _ring


def get_ring() -> RingHandler | None:
    return _ring


import sys  # noqa: E402  (used by the OSError branch above)


def _selftest() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPDATA"] = tmp
        ring = configure(level=logging.DEBUG)
        log.info("hello %s", "world")
        log.warning("careful")

        lines, version = ring.snapshot()
        assert any("hello world" in ln for ln in lines), lines
        assert any("WARNING" in ln and "careful" in ln for ln in lines), lines
        assert version == 2, version
        assert len(ring.snapshot()[0]) == 2, "ring must not duplicate entries"

        # A second configure must not stack handlers (a classic double-log bug).
        configure(level=logging.INFO)
        log.info("second")
        assert len(ring.snapshot()[0]) == 3, ring.snapshot()[0]
        assert len(log.handlers) == 2, [type(h).__name__ for h in log.handlers]

        # The ring must evict from the front, keeping the newest lines.
        ring.clear()
        for i in range(5):
            log.info("line %d", i)
        lines, _ = ring.snapshot()
        assert lines[-1].endswith("line 4"), lines[-1]
        assert len(lines) == 5, lines

        # Release file handles before the temp dir goes away: on Windows a
        # RotatingFileHandler holds its file open, and TemporaryDirectory
        # cannot delete it otherwise.
        for handler in list(log.handlers):
            handler.close()
            log.removeHandler(handler)
        _reset_for_selftest()
    print("logbus selftest OK")


def _reset_for_selftest() -> None:
    """Drop the module-level ring so a second test run starts clean."""
    global _ring
    _ring = None


if __name__ == "__main__":
    _selftest()
