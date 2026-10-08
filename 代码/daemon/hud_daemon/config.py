"""config.py — daemon settings, loaded from %APPDATA%\\ClaudeHUD\\config.json.

Kept as a plain dataclass plus explicit load/save rather than a global dict:
the Electron UI edits these fields, and an unknown-key crash there would take
the daemon down on a schema change. Unknown keys in the file are ignored, and
missing keys fall back to the defaults below.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .logbus import app_dir

# BLE identifiers. These MUST match firmware/claude_hud/config.h exactly.
# The service and RX UUIDs are deliberately unchanged from the first firmware
# build: Windows caches a device's GATT table, and changing them would force the
# user to unpair and re-pair.
SERVICE_UUID = "12345678-1234-1234-1234-123456789abc"
RX_UUID = "12345678-1234-1234-1234-123456789abd"
TX_UUID = "12345678-1234-1234-1234-123456789abe"
DEVICE_NAME = "Claude-HUD"

DEFAULT_PORT = 17321
HOOK_SENTINEL = "cchud-hook"


@dataclass(slots=True)
class Settings:
    # ── transport ──────────────────────────────────────────────
    port: int = DEFAULT_PORT

    # ── behaviour ──────────────────────────────────────────────
    # How long a transient TOOL_END holds before the state machine falls back.
    # The firmware enforces its own 1s; this is the daemon-side copy used for
    # the UI countdown, so the two must not drift far apart.
    tool_end_hold_ms: int = 1000

    # Same state hammered repeatedly inside this window is sent once. Tool
    # chains fire PreToolUse back to back far faster than the panel can show.
    dedupe_ms: int = 200

    heartbeat_s: int = 5

    # ── hooks ──────────────────────────────────────────────────
    hooks_enabled: bool = True
    hook_events: tuple[str, ...] = (
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "Notification",
    )
    hook_command: str = "cchud-hook"

    # ── expressions ────────────────────────────────────────────
    slot_count: int = 12

    # Populated by the daemon at runtime; not read from disk.
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or config_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, json.JSONDecodeError):
            # A corrupt config must not stop the daemon from starting; defaults
            # are always safe because nothing here is a secret.
            return cls()
        if not isinstance(raw, dict):
            return cls()

        known = {f for f in cls.__slots__ if f != "extra"}
        merged = {k: v for k, v in raw.items() if k in known}
        # hook_events arrives from JSON as a list; the dataclass declares a
        # tuple so it stays hashable and immutable once loaded.
        if isinstance(merged.get("hook_events"), list):
            merged["hook_events"] = tuple(merged["hook_events"])
        return cls(**merged)

    def save(self, path: Path | None = None) -> None:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("extra", None)
        if isinstance(data.get("hook_events"), tuple):
            data["hook_events"] = list(data["hook_events"])
        # Write-then-rename: a crash mid-write must not leave a truncated file
        # that the next load rejects.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


def config_path() -> Path:
    return app_dir() / "config.json"


def expressions_dir() -> Path:
    path = app_dir() / "expressions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _selftest() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPDATA"] = tmp
        path = Path(tmp) / "ClaudeHUD" / "config.json"

        s = Settings()
        s.port = 12345
        s.save(path)

        loaded = Settings.load(path)
        assert loaded.port == 12345, loaded.port
        assert isinstance(loaded.hook_events, tuple), type(loaded.hook_events)
        assert loaded.hook_events == s.hook_events

        # Unknown keys must be dropped, not crash the load.
        path.write_text('{"port": 999, "future_key": 1}', encoding="utf-8")
        again = Settings.load(path)
        assert again.port == 999, again.port
        assert not hasattr(again, "future_key")

        # Corrupt JSON falls back to defaults rather than raising.
        path.write_text("{not json", encoding="utf-8")
        assert Settings.load(path).port == DEFAULT_PORT

        # A missing file is defaults, not an error.
        assert Settings.load(Path(tmp) / "nope.json").port == DEFAULT_PORT

        # The temp file must not be left behind by a successful save.
        assert not (path.parent / "config.tmp").exists()
    print("config selftest OK")


if __name__ == "__main__":
    _selftest()
