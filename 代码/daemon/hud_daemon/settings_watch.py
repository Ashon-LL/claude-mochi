"""settings_watch.py — keep our hooks installed in Claude Code's settings.json.

The patcher in settings_patch.py can install the hooks once. This module is what
keeps them installed, which is the part that actually matters: cc-switch rewrites
~/.claude/settings.json from its own database every time the user switches
providers, and that rewrite silently deletes anything it did not put there.

Strategy, and why each piece is there:

  * Poll the file's hash every 500 ms. A real inotify/kqueue watcher would be
    more elegant, but Windows file notifications arrive with enough latency and
    enough duplicate events that polling with a hash is both simpler and more
    predictable — and this is a config file, not a hot path.
  * Compare against the hash of the bytes we last wrote. Without that guard the
    watcher sees its own write as an external change and re-applies forever.
  * Debounce 300 ms before acting. cc-switch does not write atomically, so a
    poll landing mid-write sees truncated JSON; waiting turns that into a
    successful read instead of a spurious failure.
  * A malformed file is not fatal. It is logged and retried on the next poll,
    because the next poll is half a second away and the user's Claude Code keeps
    working either way.

CLI:

    python -m hud_daemon.settings_watch --once      install and exit
    python -m hud_daemon.settings_watch --status    report current state and exit
    python -m hud_daemon.settings_watch             run as a foreground watcher
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .logbus import configure, log
from .paths import hookshim_command
from .settings_patch import SENTINEL, SettingsPatcher

POLL_S = 0.5
DEBOUNCE_S = 0.3

# Which shim the hook command installs. "exe" is the compiled NativeAOT build
# with no runtime dependency; "script" runs through Python and needs no rebuild,
# which makes it the better choice while iterating.
DEFAULT_SHIM = os.environ.get("CCHUD_SHIM", "exe")


def build_command(shim: str) -> str:
    """The command string installed into settings.json, quoted as needed.

    Delegates to paths.hookshim_command so the quoting rule lives in one place:
    this project sits under "D:\\Claude DIY\\", and an unquoted space makes the
    shell treat "D:\\Claude" as the executable — a hook that silently never
    runs, which cost a full debugging round to find.
    """
    return hookshim_command(shim)


@dataclass(slots=True)
class WatchStats:
    polls: int = 0
    external_writes: int = 0     # file changed by something other than us
    reinjected: int = 0          # our hooks were gone and we put them back
    other_hooks_preserved: int = 0
    errors: int = 0


class SettingsWatcher:
    def __init__(self, patcher: SettingsPatcher, *,
                 poll_s: float = POLL_S, debounce_s: float = DEBOUNCE_S) -> None:
        self.patcher = patcher
        self.poll_s = poll_s
        self.debounce_s = debounce_s
        self.stats = WatchStats()
        self._stop = asyncio.Event()

    # ── status for the UI ──────────────────────────────────────
    def status(self) -> dict:
        # Fields listed explicitly rather than via __dict__: the dataclass uses
        # slots, so __dict__ does not exist, and an explicit list is also a
        # stable contract for the UI.
        return {
            "path": str(self.patcher.path),
            "command": self.patcher.command,
            "enabled": self.patcher.enabled,
            "installed": self.patcher.has_ours(),
            "polls": self.stats.polls,
            "external_writes": self.stats.external_writes,
            "reinjected": self.stats.reinjected,
            "other_hooks_preserved": self.stats.other_hooks_preserved,
            "errors": self.stats.errors,
        }

    # `status_dict` is the name the HTTP layer uses, and it must not be a second
    # implementation of the same thing. Keep both names pointed at one body.
    status_dict = status

    # ── one-shot ───────────────────────────────────────────────
    def apply_once(self) -> bool:
        """Install or repair the hooks right now. Returns True on success."""
        result = self.patcher.apply()
        if result.error:
            log.error("hook install failed: %s", result.error)
            self.stats.errors += 1
            return False
        if result.changed:
            log.info("hooks installed -> %s", self.patcher.path)
        else:
            log.info("hooks already in place")
        return True

    # ── repair, called from the UI ────────────────────────────
    def repair(self):
        """Force a re-inject and report what happened.

        The panel's "修复 hook" button is the user's only recovery path after
        cc-switch wipes our hooks mid-poll, or after they edit settings.json by
        hand and delete something. It must not report success for a merge that
        failed, because the failure is invisible on the panel: the hooks would
        just stop working with no other symptom.

        Returns the patcher's own result so the caller can tell "changed",
        "already correct" and "failed" apart — those are three different things
        to show a user who just pressed a repair button.
        """
        result = self.patcher.apply()
        if result.error:
            log.error("hook repair failed: %s", result.error)
            self.stats.errors += 1
            return result
        if result.changed:
            log.info("hook repair re-injected -> %s", self.patcher.path)
        return result

    # ── the loop ───────────────────────────────────────────────
    async def run(self) -> None:
        log.info("settings watcher on %s (poll %.1fs, debounce %.1fs)",
                 self.patcher.path, self.poll_s, self.debounce_s)
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A watcher that dies takes the whole daemon with it, and this
                # is the one component whose failure is invisible until the user
                # switches providers.
                log.warning("watcher tick failed: %s", exc)
                self.stats.errors += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_s)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        self.stats.polls += 1

        current = self.patcher.file_hash()
        if current is None:
            # settings.json vanished. Claude Code will recreate it; re-inject
            # once it does, which the next polls handle on their own.
            log.debug("settings.json not present")
            return

        if current == self.patcher._last_written_hash:
            return  # our own write, nothing to do

        # Something else wrote the file.
        self.stats.external_writes += 1
        await asyncio.sleep(self.debounce_s)

        # Re-read after the debounce: cc-switch may still be mid-write.
        after = self.patcher.file_hash()
        if after is None:
            return

        had_ours = self.patcher.has_ours()
        result = self.patcher.apply()
        if result.error:
            log.warning("external write seen but re-inject failed: %s", result.error)
            self.stats.errors += 1
            return

        if result.changed:
            self.stats.reinjected += 1
            if had_ours:
                self.stats.other_hooks_preserved += 1
            log.warning("hooks were removed from settings.json; re-injected "
                        "(cc-switch switch?)")
        else:
            log.debug("external write, our hooks survived")

    def stop(self) -> None:
        self._stop.set()


def _selftest() -> None:
    """Drive the watcher against a simulated cc-switch overwrite."""
    import json
    import tempfile

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")
            patcher = SettingsPatcher(path, r"C:\tools\cchud-hook.exe",
                                      backup_dir=Path(tmp) / "bk")
            watcher = SettingsWatcher(patcher, poll_s=0.05, debounce_s=0.05)

            # Install, then let a tick run: it must not re-apply.
            assert watcher.apply_once()
            baseline = path.read_text(encoding="utf-8")
            await watcher._tick()
            assert path.read_text(encoding="utf-8") == baseline, \
                "watcher must not rewrite a file it already owns"
            assert watcher.stats.reinjected == 0, watcher.stats

            # cc-switch wipes the file with its own content.
            path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://x"},
                                        "model": "opus"}), encoding="utf-8")
            await watcher._tick()
            after = json.loads(path.read_text(encoding="utf-8"))
            assert after["model"] == "opus", "cc-switch's content must survive"
            assert after["env"]["ANTHROPIC_BASE_URL"] == "http://x"
            assert SENTINEL in json.dumps(after), "our hooks must be restored"
            assert watcher.stats.reinjected == 1, watcher.stats

            # A second tick on the now-stable file must be quiet.
            await watcher._tick()
            assert watcher.stats.reinjected == 1, watcher.stats

            # Disabling removes ours and leaves the rest alone.
            patcher.enabled = False
            patcher.apply()
            assert not patcher.has_ours()
            assert json.loads(path.read_text(encoding="utf-8"))["model"] == "opus"
            print("settings_watch selftest OK")

    asyncio.run(scenario())


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hud_daemon.settings_watch")
    parser.add_argument("--once", action="store_true",
                        help="install the hooks and exit")
    parser.add_argument("--status", action="store_true",
                        help="print the current state and exit")
    parser.add_argument("--selftest", action="store_true",
                        help="run the built-in tests against a temp settings.json "
                             "and exit; never touches the real config")
    parser.add_argument("--shim", choices=("exe", "script"), default=DEFAULT_SHIM,
                        help="'exe' installs the compiled NativeAOT shim (no "
                             "runtime dependency); 'script' runs it through "
                             "Python, which needs no rebuild")
    parser.add_argument("--settings", default="",
                        help="path to settings.json (default: ~/.claude/settings.json)")
    parser.add_argument("--enable", dest="enabled", action="store_true", default=True)
    parser.add_argument("--disable", dest="enabled", action="store_false")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    configure(level=10 if args.debug else 20)

    # A console handler is required for the CLI modes. The ring and file
    # handlers that configure() installs are for the long-running daemon; with
    # only those, --once and --status print nothing at all and the caller has
    # no way to tell whether the injection worked.
    import logging

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                           datefmt="%H:%M:%S"))
    log.addHandler(console)

    # An explicit flag, because an earlier revision called _selftest() nowhere
    # and instead ran the watcher by default, which silently installed hooks
    # into the live settings.json.
    if args.selftest:
        _selftest()
        return 0

    path = Path(args.settings) if args.settings else Path.home() / ".claude" / "settings.json"
    command = build_command(args.shim)
    patcher = SettingsPatcher(path, command, enabled=args.enabled)
    watcher = SettingsWatcher(patcher)

    if args.status:
        import json
        print(json.dumps(watcher.status(), indent=2, ensure_ascii=False))
        return 0

    if args.once:
        return 0 if watcher.apply_once() else 1

    # Watcher mode edits the real settings.json. Announce it before doing so:
    # the failure mode this guards against is a silent modification of live
    # config, and the only recovery was a backup whose name the user never saw.
    log.warning("WATCHER MODE: this will modify %s", path)
    log.warning("  use --once to install and exit, --status to inspect, "
                "--selftest to run tests")
    try:
        asyncio.run(watcher.run())
    except KeyboardInterrupt:
        watcher.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
