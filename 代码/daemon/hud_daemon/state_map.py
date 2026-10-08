"""state_map.py — Claude Code hook events mapped to HUD states.

Pure logic, no I/O, so the mapping can be reasoned about and tested on its own.
Everything that decides "what should the panel show" lives here; the transport
layers just carry the result.

Two rules drive the design, both learned from watching the real event stream:

  * PreToolUse and PostToolUse arrive in bursts. A single Claude turn doing five
    tool calls fires the pair five times inside a couple of seconds. Sending
    every one would make the panel flicker between TOOL_START and TOOL_END for
    no visible reason. So a repeated identical event inside dedupe_ms is
    dropped, and TOOL_END is held rather than immediately replaced.

  * PostToolUse does not mean "finished". Claude is usually already running the
    next tool. So TOOL_END is transient: it shows for tool_end_hold_ms and then
    falls back to THINKING, which matches what the terminal shows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import protocol as P

# Claude Code hook events this build understands. Anything else is ignored
# rather than mapped to a state, so a new event type degrades to "no change"
# instead of a wrong face.
HOOK_TO_STATE = {
    "UserPromptSubmit": P.ST_THINKING,
    "PreToolUse": P.ST_TOOL_START,
    "PostToolUse": P.ST_TOOL_END,
    "Stop": P.ST_IDLE,
    "Notification": P.ST_WAITING,
}


@dataclass(slots=True)
class Decision:
    """What the caller should do with a hook event."""
    send: bool
    state: int | None = None
    reason: str = ""

    @classmethod
    def no(cls, reason: str) -> "Decision":
        return cls(send=False, reason=reason)


class StateMapper:
    def __init__(self, *, dedupe_ms: int = 200, tool_end_hold_ms: int = 1000) -> None:
        self._dedupe_s = dedupe_ms / 1000.0
        self._tool_end_hold_s = tool_end_hold_ms / 1000.0

        self._last_state: int | None = None
        self._last_sent_at = 0.0
        self._tool_end_at: float | None = None

    # ── main entry point ───────────────────────────────────────
    def on_hook_event(self, event_name: str, now: float | None = None) -> Decision:
        """Map one hook event to a state change, or explain why not."""
        now = time.monotonic() if now is None else now

        target = HOOK_TO_STATE.get(event_name)
        if target is None:
            return Decision.no(f"unmapped event '{event_name}'")

        if target == P.ST_TOOL_END:
            # Transient: show it, then fall back on its own.
            self._tool_end_at = now + self._tool_end_hold_s
            return self._emit(target, now, "tool_end")

        # Any other state supersedes a pending revert. Without this, a Stop
        # landing right after PostToolUse would be overwritten by the fallback
        # a second later, and the panel would bounce IDLE -> THINKING for no
        # reason the user can explain.
        self._tool_end_at = None

        if self._last_state == target and (now - self._last_sent_at) < self._dedupe_s:
            return Decision.no(f"deduped '{event_name}'")

        return self._emit(target, now, event_name)

    # ── the transient revert ───────────────────────────────────
    def poll(self, now: float | None = None) -> Decision:
        """Call regularly. Returns the fallback decision once TOOL_END expires.

        Exposed as an explicit poll rather than a timer so the caller controls
        when we send: a timer inside the mapper would need its own task and
        would fire whether or not the link is up.
        """
        now = time.monotonic() if now is None else now
        if self._tool_end_at is None or now < self._tool_end_at:
            return Decision.no("no pending revert")
        self._tool_end_at = None
        return self._emit(P.ST_THINKING, now, "tool_end_revert")

    # ── introspection for the UI ───────────────────────────────
    @property
    def last_state(self) -> int | None:
        return self._last_state

    @property
    def pending_revert(self) -> bool:
        return self._tool_end_at is not None

    def reset(self) -> None:
        self._last_state = None
        self._last_sent_at = 0.0
        self._tool_end_at = None

    # ── internals ──────────────────────────────────────────────
    def _emit(self, state: int, now: float, reason: str) -> Decision:
        self._last_state = state
        self._last_sent_at = now
        return Decision(send=True, state=state, reason=reason)


def _selftest() -> None:
    m = StateMapper(dedupe_ms=200, tool_end_hold_ms=1000)

    # Every mapped event reaches a state.
    d = m.on_hook_event("UserPromptSubmit", now=0.0)
    assert d.send and d.state == P.ST_THINKING, d
    d = m.on_hook_event("PreToolUse", now=0.1)
    assert d.send and d.state == P.ST_TOOL_START, d
    d = m.on_hook_event("PostToolUse", now=0.2)
    assert d.send and d.state == P.ST_TOOL_END, d
    d = m.on_hook_event("Stop", now=0.3)
    assert d.send and d.state == P.ST_IDLE, d
    d = m.on_hook_event("Notification", now=0.4)
    assert d.send and d.state == P.ST_WAITING, d

    # An unmapped event changes nothing.
    d = m.on_hook_event("SessionStart", now=0.5)
    assert not d.send and "unmapped" in d.reason, d

    # A repeated identical event inside the window is dropped...
    m2 = StateMapper(dedupe_ms=200)
    m2.on_hook_event("PreToolUse", now=1.0)
    d = m2.on_hook_event("PreToolUse", now=1.1)
    assert not d.send and "deduped" in d.reason, d
    # ...but the same event after the window goes through.
    d = m2.on_hook_event("PreToolUse", now=1.5)
    assert d.send, d

    # TOOL_END holds then falls back to THINKING, not IDLE.
    m3 = StateMapper(tool_end_hold_ms=1000)
    m3.on_hook_event("PreToolUse", now=2.0)
    m3.on_hook_event("PostToolUse", now=2.1)
    assert m3.pending_revert
    d = m3.poll(now=2.5)
    assert not d.send, d                       # still holding
    d = m3.poll(now=3.2)
    assert d.send and d.state == P.ST_THINKING, d
    assert not m3.pending_revert
    d = m3.poll(now=3.3)
    assert not d.send, d                       # only fires once

    # A new event cancels a pending revert instead of stacking two states.
    m4 = StateMapper(tool_end_hold_ms=1000)
    m4.on_hook_event("PostToolUse", now=4.0)
    d = m4.on_hook_event("Stop", now=4.1)
    assert d.send and d.state == P.ST_IDLE, d
    assert not m4.pending_revert, "revert must be cancelled by a new event"
    d = m4.poll(now=5.0)
    assert not d.send, d
    print("state_map selftest OK")


if __name__ == "__main__":
    _selftest()
