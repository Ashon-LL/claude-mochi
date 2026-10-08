// state_machine.h — what the HUD should be showing right now.
//
// Pure logic, no display and no BLE calls, so it can be reasoned about (and
// host-tested) on its own. The renderer asks visible() and draws that.
//
// Two rules drive everything here:
//
//   TOOL_END is transient. PostToolUse fires at the end of a single tool call,
//   but Claude is usually still working on the next one. Holding the checkmark
//   for a second and then falling back to THINKING matches what the user sees
//   in the terminal; leaving it up would make the HUD look stuck.
//
//   OFFLINE is derived, never received. If no frame arrives for HOST_TIMEOUT_MS
//   the host is gone (crashed, laptop slept, cable pulled). The firmware must
//   decide this itself, otherwise the screen keeps showing a stale "thinking"
//   face forever and the user has no way to tell the link died.
#pragma once
#include <Arduino.h>
#include "config.h"

class StateMachine {
public:
  // Host-link state starts OFFLINE. lastHostMs_ is set to (now - timeout) so
  // the first tick() resolves to offline rather than assuming the daemon is
  // alive just because setup() ran: a freshly flashed device has no host at
  // all, and showing IDLE in that state is what made the original build
  // unreadable after a reboot.
  void begin(uint32_t now) {
    current_       = ST_IDLE;
    hostOnline_    = false;
    lastHostMs_    = now - HOST_TIMEOUT_MS - 1;
    toolEndAtMs_   = 0;
    toolEndRevert_ = ST_THINKING;
  }

  // A STATE frame arrived from the daemon.
  // Returns true when the visible state changed.
  bool onHostState(uint8_t s, uint32_t now) {
    if (s >= ST_COUNT) return false;
    lastHostMs_  = now;
    hostOnline_  = true;

    HudState next = (HudState)s;
    if (next == ST_TOOL_END) {
      toolEndAtMs_   = now + TOOL_END_HOLD_MS;
      toolEndRevert_ = ST_THINKING;
    } else {
      toolEndAtMs_ = 0;
    }
    return assign(next);
  }

  // Any frame at all proves the host is alive, even one we ignore.
  void noteHostActivity(uint32_t now) {
    lastHostMs_ = now;
    hostOnline_ = true;
  }

  // Call every loop iteration. Returns true when the visible state changed,
  // i.e. the caller should repaint.
  bool tick(uint32_t now) {
    bool changed = false;

    if (toolEndAtMs_ && (int32_t)(now - toolEndAtMs_) >= 0) {
      toolEndAtMs_ = 0;
      changed |= assign(toolEndRevert_);
    }

    const bool online = (now - lastHostMs_) < HOST_TIMEOUT_MS;
    if (online != hostOnline_) {
      hostOnline_ = online;
      changed = true;   // the badge and OFFLINE face both depend on this
    }

    return changed;
  }

  // What to draw: the logical state, unless the host went away.
  HudState visible() const { return hostOnline_ ? current_ : ST_OFFLINE; }

  HudState logical() const { return current_; }
  bool     hostOnline() const { return hostOnline_; }

  // Human-readable label for the bottom status strip. Kept ASCII: the default
  // GFX font has no CJK glyphs and P0 ships without a custom font.
  const char* stateName() const {
    switch (visible()) {
      case ST_IDLE:       return "IDLE";
      case ST_THINKING:   return "THINKING";
      case ST_TOOL_START: return "TOOL";
      case ST_TOOL_END:   return "DONE";
      case ST_WAITING:    return "WAITING";
      case ST_ERROR:      return "ERROR";
      case ST_OFFLINE:    return "NO HOST";
      default:            return "?";
    }
  }

private:
  bool assign(HudState s) {
    if (s == current_) return false;
    current_ = s;
    return true;
  }

  HudState current_;
  bool     hostOnline_;
  uint32_t lastHostMs_;
  uint32_t toolEndAtMs_;    // 0 = not in the transient TOOL_END hold
  HudState toolEndRevert_;
};
