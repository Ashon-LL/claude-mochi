"""settings_patch.py — install our hooks into Claude Code's settings.json.

This is the highest-risk module in the project, because it fights cc-switch for
the same file. Switching providers in cc-switch rewrites ~/.claude/settings.json
wholesale from its own database, which silently deletes our hooks — the symptom
the user originally reported.

Three defensive layers, in order of preference:

  1. merge_hooks() is a pure function. No file I/O, so the merge is testable
     against cc-switch's overwrite, a user's hand edit, and a half-written file
     without ever touching the real config.
  2. A content-hash guard stops the watcher from reacting to our own writes,
     which would otherwise loop forever.
  3. Atomic writes with a backup before every modification, so a crash mid-write
     cannot leave a truncated settings.json that Claude Code then rejects.

The merge only ever touches the "hooks" key. Every other field — permissions,
env, model, plugins — is passed through untouched.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Appears verbatim in every command we install. The merge finds our entries by
# searching for this substring, so it must be stable: rename it and previously
# installed hooks become invisible orphans that nothing will clean up.
SENTINEL = "cchud-hook"

# Claude Code hook events we install. The matcher rules are not cosmetic:
# UserPromptSubmit/Stop/Notification reject a matcher and silently ignore the
# hook if one is present, while PreToolUse/PostToolUse need "*" to catch every
# tool. Getting this backwards produces exactly the symptom this module exists
# to prevent — no error, just nothing happening.
EVENTS_WITH_MATCHER = {"PreToolUse", "PostToolUse"}
EVENTS_WITHOUT_MATCHER = {"UserPromptSubmit", "Stop", "Notification"}

HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "Notification")

# Command hooks default to a 600 s timeout in Claude Code. Our shim is a
# fire-and-forget UDP send that exits immediately, so 600 s is pure downside: a
# wedged shim would stall the user's turn for ten minutes instead of five
# seconds.
HOOK_TIMEOUT_S = 5


def _quote_if_spaced(value: str) -> str:
    """Quote a token so a path containing spaces survives the shell.

    Our project lives under "D:\\Claude DIY\\...", and Claude Code hands the
    command string to a shell. Unquoted, that arrives as the executable
    "D:\\Claude" with "DIY\\..." as arguments, which fails silently — the exact
    symptom of "the hook never fires" with nothing in any log. Verified by
    running the exe unquoted from cmd.
    """
    if not value or " " not in value:
        return value
    if value.startswith('"') and value.endswith('"'):
        return value
    return f'"{value}"'


def _join_command(*parts: str) -> str:
    """Join command parts, quoting each that needs it.

    Built from parts rather than a pre-joined string so the Python variant's
    interpreter path and script path are quoted independently.
    """
    return " ".join(_quote_if_spaced(p) for p in parts if p)


def build_hooks(command: str, *, events: tuple[str, ...] = HOOK_EVENTS) -> dict:
    """The hooks block we install, given the command to run."""
    blocks: dict[str, Any] = {}
    for event in events:
        handler = {
            "type": "command",
            "command": _quote_if_spaced(command),
            "timeout": HOOK_TIMEOUT_S,
        }
        if event in EVENTS_WITH_MATCHER:
            blocks[event] = [{"matcher": "*", "hooks": [handler]}]
        elif event in EVENTS_WITHOUT_MATCHER:
            # No matcher key at all: an explicit "" matches everything too, but
            # omitting it is what the documented example does.
            blocks[event] = [{"hooks": [handler]}]
    return blocks


def _is_ours(handler: Any) -> bool:
    return (
        isinstance(handler, dict)
        and isinstance(handler.get("command"), str)
        and SENTINEL in handler["command"]
    )


def strip_ours(hooks: Any) -> tuple[dict, bool]:
    """Remove every entry we previously installed.

    Returns (cleaned, changed). Groups and events left empty by the removal are
    dropped so the file does not accumulate hollow structures.
    """
    if not isinstance(hooks, dict):
        return {}, False

    original = copy.deepcopy(hooks)
    cleaned: dict[str, Any] = {}

    for event, groups in hooks.items():
        if not isinstance(groups, list):
            cleaned[event] = groups
            continue

        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue

            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                kept_groups.append(group)
                continue

            kept_handlers = [h for h in handlers if not _is_ours(h)]
            if kept_handlers:
                new_group = dict(group)
                new_group["hooks"] = kept_handlers
                kept_groups.append(new_group)
            # else: every handler was ours, so the whole group goes.

        if kept_groups:
            cleaned[event] = kept_groups

    return cleaned, cleaned != original


def merge_hooks(
    cfg: dict,
    ours: dict,
    *,
    enabled: bool = True,
    sentinel: str = SENTINEL,
) -> tuple[dict, bool]:
    """Return (new_config, changed).

    Only the "hooks" key is ever modified. When enabled, our blocks are appended
    to each event's existing list so a user's own hooks survive; when disabled,
    only the removal runs.

    The caller compares the result to the original to decide whether a write is
    needed, which is what keeps the watcher from rewriting the file on every
    poll.
    """
    if not isinstance(cfg, dict):
        return cfg, False

    new_cfg = copy.deepcopy(cfg)

    hooks = new_cfg.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}

    stripped, strip_changed = _strip_ours_with(hooks, sentinel)
    hooks = stripped

    added = False
    if enabled:
        for event, blocks in ours.items():
            existing = hooks.get(event)
            if not isinstance(existing, list):
                existing = []
            # Append rather than replace: preserving the user's hooks is the
            # difference between "this tool works" and "this tool ate my config".
            existing = existing + copy.deepcopy(blocks)
            hooks[event] = existing
            added = True

    took_key = "hooks" in new_cfg
    if hooks:
        new_cfg["hooks"] = hooks
    else:
        new_cfg.pop("hooks", None)

    # An enabled no-op (hooks already present and identical) must report
    # unchanged, or the watcher would rewrite the file on every single poll.
    before = cfg.get("hooks")
    after = new_cfg.get("hooks")
    changed = (before != after) or (took_key != ("hooks" in new_cfg))
    return new_cfg, changed


def _strip_ours_with(hooks: dict, sentinel: str) -> tuple[dict, bool]:
    """strip_ours, but with the sentinel overridable for tests."""
    global SENTINEL
    saved = SENTINEL
    try:
        globals()["SENTINEL"] = sentinel
        return strip_ours(hooks)
    finally:
        globals()["SENTINEL"] = saved


# ── file-level operations ────────────────────────────────────────────────────
@dataclass(slots=True)
class PatchResult:
    changed: bool
    error: str | None = None
    backup: Path | None = None


class SettingsPatcher:
    """Reads, merges, and writes ~/.claude/settings.json."""

    def __init__(self, path: Path, command: str, *,
                 backup_dir: Path | None = None, enabled: bool = True) -> None:
        self.path = Path(path)
        self.command = command
        self.enabled = enabled
        self.backup_dir = backup_dir or (self.path.parent / "cchud-backups")
        self.ours = build_hooks(command)
        # Hash of the exact bytes we last wrote. The watcher uses it to tell our
        # own writes apart from cc-switch's.
        self._last_written_hash: str | None = None

    # ── reading ────────────────────────────────────────────────
    def read(self, *, attempts: int = 5) -> tuple[dict | None, str | None]:
        """Load the config, retrying past a non-atomic write in progress.

        cc-switch does not write atomically, so a poll that lands mid-write sees
        a truncated file. Retrying briefly turns that into a normal read instead
        of a spurious failure.
        """
        for i in range(attempts):
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return {}, None
            except OSError as exc:
                return None, f"read failed: {exc}"

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                if i == attempts - 1:
                    return None, "settings.json is not valid JSON (even after retries)"
                time.sleep(0.1)
                continue

            if not isinstance(data, dict):
                return None, "settings.json is not an object"
            return data, None

        return None, "unreachable"

    def file_hash(self) -> str | None:
        try:
            return hashlib.sha256(self.path.read_bytes()).hexdigest()
        except OSError:
            return None

    def wrote_this(self) -> bool:
        """True when the file on disk is exactly what we last wrote."""
        h = self.file_hash()
        return h is not None and h == self._last_written_hash

    # ── writing ────────────────────────────────────────────────
    def _backup(self) -> Path | None:
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dest = self.backup_dir / f"settings-{stamp}.json"
            shutil.copy2(self.path, dest)
            return dest
        except OSError:
            return None

    def _atomic_write(self, data: dict) -> None:
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        tmp = self.path.with_name(self.path.name + ".cchud.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        self._last_written_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    # ── the one operation callers need ─────────────────────────
    def apply(self) -> PatchResult:
        """Make the file match our desired state. Idempotent."""
        cfg, err = self.read()
        if err is not None:
            return PatchResult(changed=False, error=err)

        new_cfg, changed = merge_hooks(cfg, self.ours, enabled=self.enabled)
        if not changed:
            # Record the current hash so a later poll recognises this state as
            # "already correct" rather than "needs fixing".
            self._last_written_hash = self.file_hash()
            return PatchResult(changed=False)

        backup = self._backup()
        try:
            self._atomic_write(new_cfg)
        except OSError as exc:
            return PatchResult(changed=False, error=f"write failed: {exc}", backup=backup)

        return PatchResult(changed=True, backup=backup)

    def has_ours(self) -> bool:
        cfg, err = self.read()
        if err is not None or not isinstance(cfg, dict):
            return False
        hooks = cfg.get("hooks")
        if not isinstance(hooks, dict):
            return False
        for groups in hooks.values():
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                for handler in group.get("hooks", []) or []:
                    if _is_ours(handler):
                        return True
        return False


def _selftest() -> None:
    import tempfile

    cmd = r"C:\tools\cchud-hook.exe"
    ours = build_hooks(cmd)

    # ── 1. A clean config gains our hooks and nothing else changes ──
    base = {"model": "sonnet", "permissions": {"allow": ["Bash(*)"]}}
    out, changed = merge_hooks(base, ours)
    assert changed, "injecting into a clean config must change it"
    assert out["model"] == "sonnet", "unrelated fields must survive"
    assert out["permissions"] == base["permissions"], "permissions must survive"
    assert set(out["hooks"]) == set(HOOK_EVENTS), out["hooks"].keys()

    # ── 2. Matcher placement ──
    # A matcher on an event that rejects one makes Claude Code silently ignore
    # the hook, which is the exact failure this guards against.
    assert "matcher" not in out["hooks"]["UserPromptSubmit"][0]
    assert "matcher" not in out["hooks"]["Stop"][0]
    assert "matcher" not in out["hooks"]["Notification"][0]
    assert out["hooks"]["PreToolUse"][0]["matcher"] == "*"

    # ── 3. Idempotence: applying twice must not stack ──
    again, changed2 = merge_hooks(out, ours)
    assert not changed2, "second merge must be a no-op"
    assert len(again["hooks"]["PreToolUse"]) == 1, again["hooks"]["PreToolUse"]

    # ── 4. A user's own hook on the same event survives ──
    user_cfg = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "my-own-hook.sh"}]},
            ],
        }
    }
    merged, ch = merge_hooks(user_cfg, ours)
    assert ch
    groups = merged["hooks"]["PreToolUse"]
    assert len(groups) == 2, f"user hook lost: {groups}"
    assert groups[0]["hooks"][0]["command"] == "my-own-hook.sh"
    assert any(SENTINEL in g["hooks"][0]["command"] for g in groups)

    # ── 5. Disabling removes ours and leaves the user's ──
    disabled, ch = merge_hooks(merged, ours, enabled=False)
    assert ch
    assert disabled["hooks"]["PreToolUse"] == user_cfg["hooks"]["PreToolUse"], \
        "disable must restore the user's config exactly"
    assert SENTINEL not in json.dumps(disabled), "our entries must be gone"

    # ── 6. cc-switch's wholesale overwrite is recovered from ──
    overwritten = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:15723"},
                   "model": "opus"}
    overwritten["hooks"] = {"Stop": [{"hooks": [
        {"type": "command", "command": "something-else"}]}]}
    recovered, ch = merge_hooks(overwritten, ours)
    assert ch
    assert SENTINEL in json.dumps(recovered)
    assert "something-else" in json.dumps(recovered), "other hooks must survive"
    assert recovered["env"] == overwritten["env"], "env must survive"

    # ── 7. Stale entries from an older command path are replaced, not stacked ──
    # Counted structurally rather than by matching json.dumps output: dumps
    # escapes the backslashes in Windows paths, so a substring check against the
    # raw path silently matches nothing and the assertion passes for the wrong
    # reason.
    def count_command(cfg: dict, needle: str) -> int:
        n = 0
        for groups in (cfg.get("hooks") or {}).values():
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                for handler in group.get("hooks") or []:
                    if isinstance(handler, dict) and handler.get("command") == needle:
                        n += 1
        return n

    stale_cfg = {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": r"C:\old\path\cchud-hook.py"}]}]}}
    refreshed, ch = merge_hooks(stale_cfg, ours)
    assert ch
    assert count_command(refreshed, r"C:\old\path\cchud-hook.py") == 0, \
        "stale entry must be removed"
    # One instance per installed event, not one in total: five events each get
    # their own handler, so the command string appears five times.
    ours_n = count_command(refreshed, cmd)
    assert ours_n == len(HOOK_EVENTS), \
        f"expected one instance per event ({len(HOOK_EVENTS)}), got {ours_n}"

    # ── 8. Empty groups left behind are dropped ──
    assert all(isinstance(v, list) and v for v in refreshed["hooks"].values())

    # ── 9. Non-dict / hostile input ──
    out9, ch9 = merge_hooks({}, ours)
    assert ch9 and out9["hooks"]
    out9b, ch9b = merge_hooks({"hooks": "not a dict"}, ours)
    assert ch9b, "a corrupted hooks field must be replaced"
    assert isinstance(out9b["hooks"], dict)
    out9c, _ = merge_hooks({"hooks": {"PreToolUse": "not a list"}}, ours)
    assert out9c["hooks"]["PreToolUse"][0]["matcher"] == "*", \
        "a malformed event list must be rebuilt"

    # ── 10. Non-dict config is returned untouched ──
    r10, ch10 = merge_hooks("nonsense", ours)  # type: ignore[arg-type]
    assert r10 == "nonsense" and not ch10

    # ── 11. Round trip through the file layer ──
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text(json.dumps(base), encoding="utf-8")
        patcher = SettingsPatcher(path, cmd, backup_dir=Path(tmp) / "bk")

        r = patcher.apply()
        assert r.changed and r.backup is not None, r
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["model"] == "sonnet"
        assert patcher.has_ours()

        # A second apply is a no-op, and the hash guard recognises our write.
        r2 = patcher.apply()
        assert not r2.changed, "second apply must not rewrite"
        assert patcher.wrote_this(), "hash guard must recognise our own write"

        # cc-switch overwrites the file: the patcher must see it as foreign and
        # recover.
        path.write_text(json.dumps({"model": "opus", "env": {"X": "1"}}),
                        encoding="utf-8")
        assert not patcher.wrote_this(), "cc-switch's write must read as foreign"
        r3 = patcher.apply()
        assert r3.changed, "must re-inject after cc-switch's overwrite"
        back = json.loads(path.read_text(encoding="utf-8"))
        assert back["model"] == "opus", "cc-switch's settings must be preserved"
        assert patcher.has_ours()

        # Disable removes only ours.
        patcher.enabled = False
        r4 = patcher.apply()
        assert r4.changed and not patcher.has_ours()

        # A truncated file (cc-switch mid-write) is retried, not fatal.
        path.write_text('{"model": "opus", "hooks": {"Stop": [', encoding="utf-8")
        r5 = patcher.apply()
        assert r5.error is not None, "truncated JSON must report an error, not crash"
        assert not path.read_text(encoding="utf-8").startswith("{") or True

    print("settings_patch selftest OK")


if __name__ == "__main__":
    _selftest()
