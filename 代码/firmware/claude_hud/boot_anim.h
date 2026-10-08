// boot_anim.h — the start-up animation, streamed from LittleFS.
//
// Why this is not an expression. A face is at most MAX_PRIMS primitives, because
// each Prim is ~78 bytes and the firmware holds fourteen Expressions statically
// (seven compiled-in, seven loaded from slots). The start-up logo is 162
// segments. Storing it as an expression would push MAX_PRIMS to 172 and the
// static footprint to roughly 215 KB, on a chip with about 400 KB of SRAM that
// also runs the BLE stack. It does not fit, and it does not need to: the logo is
// a fixed sequence, so it is read a few entries at a time from flash and thrown
// away. RAM cost is one small buffer, whatever the logo's size.
//
// The files it reads, written by tools\mochi_to_boot.py:
//
//   /boot/meta.json   timing and colours (duration, hold, loop, bg, colour)
//   /boot/segs.bin    4 x int16 LE per segment  — drawn one per step
//   /boot/tris.bin    6 x int16 LE per triangle — the final filled logo
//
// Non-blocking by construction. Everything below setup() is driven by tick()
// with a millis() clock: no delay(), because a blocking boot starves the BLE
// stack and the renderer, which is the failure mode renderer.h warns about.
//
// Header-only, like the rest of this firmware.
#pragma once
#include <Arduino.h>
#include <LittleFS.h>
#include <ArduinoJson.h>
#include "config.h"
#include "expression.h"    // exprParseColor, shared with the face loader
#include "renderer.h"      // drawRawLine / drawRawTriangle, and rgb565()

// One rectangular tile of the logo, streamed in and drawn. Sized so a 512-byte
// read covers a whole batch regardless of entry size, and so the buffer is a
// static allocation rather than heap: heap pressure during boot is exactly when
// an allocation is most likely to fail.
// Entry sizes for the streamed files. Paths (BOOT_PATH and friends) live in
// config.h, because store.h writes these same files and is included before this
// header — a macro defined here would not be visible to it.
#define BOOT_SEG_BYTES    8
#define BOOT_TRI_BYTES    12

// Boot phases, in order. Each is one call to a tick function from loop().
enum BootPhase : uint8_t {
  BOOT_IDLE = 0,     // no animation: either disabled, or already finished
  BOOT_TEXT,         // the product name, held briefly
  BOOT_REVEAL,       // segments appear one at a time
  BOOT_FILL,         // the filled logo appears
  BOOT_HOLD,         // stay put before handing over to the caller
};

class BootAnim {
public:
  // Values come from meta.json, with these as the defaults when it is absent or
  // unreadable. A missing file must not stop the panel booting: falling back to
  // the compiled-in timing is better than a dark screen, and the daemon can
  // upload a replacement over BLE.
  struct Meta {
    uint32_t duration_ms = 1600;
    uint32_t hold_ms     = 1200;
    bool     loop        = false;
    uint16_t bg          = 0;
    uint16_t color       = 0;
    uint16_t segments     = 0;
    uint16_t triangles    = 0;
  };

  void begin(Renderer& rr, uint16_t fallback_bg) {
    rr_ = &rr;
    meta_.bg = fallback_bg;
    meta_.color = rgb565(255, 255, 255);
    loadMeta();
  }

  // Re-read every file from disk and report what happened, as a short string the
  // host turns into a log line. "" means it loaded and is usable.
  //
  // Exists because "the upload said OK but the panel shows nothing" is
  // otherwise unanswerable: the file could be truncated, unparseable, or simply
  // absent, and all three produce a silent dark boot.
  const char* reload();

  bool available() const { return segments_ > 0; }

  // Start the sequence. Returns immediately; progress is driven by tick().
  void start(uint32_t now) {
    if (!available()) return;
    phase_      = BootPhase::BOOT_TEXT;
    phase_since_ = now;
    drawn_       = 0;
    loops_left_  = meta_.loop ? 2 : 1;   // a looping boot still ends eventually
    rr_->setBackground(meta_.bg);
    rr_->invalidate();
  }

  // True while the animation owns the panel. The caller keeps polling link and
  // storage but must not draw a face until this goes false.
  bool active() const { return phase_ != BootPhase::BOOT_IDLE; }

  BootPhase phase() const { return phase_; }

  // Drive one step. Call from loop() every iteration; internally rate-limits.
  void tick(uint32_t now);

  // Skip to the end of the sequence. Used when a hook state arrives during
  // boot: the user's work is more important than the logo.
  void skip(uint32_t now) { (void)now; phase_ = BootPhase::BOOT_IDLE; }

private:
  void loadMeta();
  bool openSegs();
  void drawText(uint32_t now);
  void drawReveal();
  void drawFill();
  void advance(uint32_t now);

  Renderer* rr_ = nullptr;
  Meta      meta_;

  File     segs_;
  File     tris_;
  // One segment or one triangle at a time. Fixed size so the whole object is
  // allocatable statically — the opening comment is the reason this file exists.
  int16_t  seg_buf_[4];
  int16_t  tri_buf_[6];
  uint16_t segments_ = 0;      // total, from meta.json
  uint16_t triangles_ = 0;
  uint16_t drawn_ = 0;         // segments already drawn this pass
  uint8_t  loops_left_ = 0;
  uint16_t step_ms_ = 10;      // derived from duration_ms / segments
  uint32_t phase_since_ = 0;
  uint32_t last_step_ = 0;
  BootPhase phase_ = BootPhase::BOOT_IDLE;
};

// Re-read every file and report what happened. "" means loaded and usable.
inline const char* BootAnim::reload() {
  static char why[56];      // static: the caller sends this over BLE after we
                            // return, so it must outlive the call
  why[0] = '\0';

  // Close anything already open: a file replaced underneath us must not be
  // served from a stale handle.
  if (segs_) { segs_.close(); }
  if (tris_) { tris_.close(); }

  const uint16_t segments_before = segments_;
  segments_ = 0;
  triangles_ = 0;
  loadMeta();

  if (!LittleFS.exists(BOOT_META)) {
    snprintf(why, sizeof(why), "boot: no meta.json, animation disabled");
    return why;
  }
  if (!LittleFS.exists(BOOT_SEGS)) {
    snprintf(why, sizeof(why), "boot: segs.bin missing");
    return why;
  }
  if (segments_ == 0) {
    snprintf(why, sizeof(why), "boot: segs.bin is empty (%u -> %u)",
             segments_before, segments_);
    return why;
  }
  if (!LittleFS.exists(BOOT_TRIS) && triangles_ != 0) {
    snprintf(why, sizeof(why), "boot: tris.bin missing");
    return why;
  }

  snprintf(why, sizeof(why), "boot: %u segments, %u triangles, step %u ms",
           segments_, triangles_, step_ms_);
  return why;
}

inline void BootAnim::loadMeta() {
  File f = LittleFS.open(BOOT_META, "r");
  if (!f) return;                    // defaults stand
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, f);
  f.close();
  if (err) return;

  // Clamp rather than reject. A hand-edited meta.json with a duration of 0
  // would otherwise divide by zero below, and a 60-second hold would look like
  // a hung boot.
  meta_.duration_ms = (uint32_t)(doc["duration_ms"] | meta_.duration_ms);
  meta_.hold_ms     = (uint32_t)(doc["hold_ms"] | meta_.hold_ms);
  meta_.loop        = doc["loop"] | false;
  meta_.segments    = (uint16_t)(doc["segments"] | 0);
  meta_.triangles   = (uint16_t)(doc["triangles"] | 0);
  meta_.duration_ms = meta_.duration_ms == 0 ? 1600 : meta_.duration_ms;
  if (meta_.duration_ms > 60000) meta_.duration_ms = 60000;
  if (meta_.hold_ms > 10000)     meta_.hold_ms = 10000;

  const char* bg = doc["bg"] | nullptr;
  const char* col = doc["color"] | nullptr;
  if (bg)  meta_.bg = exprParseColor(bg, meta_.bg);
  if (col) meta_.color = exprParseColor(col, meta_.color);

  // Read the count from the file itself when meta.json does not state it: the
  // file is the authority on its own size, and a stale count in meta.json would
  // truncate or over-run the reveal.
  File s = LittleFS.open(BOOT_SEGS, "r");
  if (s) {
    if (meta_.segments == 0) {
      meta_.segments = (uint16_t)(s.size() / BOOT_SEG_BYTES);
    }
    s.close();
  }
  File t = LittleFS.open(BOOT_TRIS, "r");
  if (t) {
    if (meta_.triangles == 0) {
      meta_.triangles = (uint16_t)(t.size() / BOOT_TRI_BYTES);
    }
    t.close();
  }
  segments_ = meta_.segments;
  triangles_ = meta_.triangles;

  // One segment per step. Guarded because a zero count would be a divide by
  // zero, and a step of 0ms would draw the whole logo in one frame — which is
  // not a reveal, it is a flash.
  if (segments_ > 0) {
    uint32_t step = meta_.duration_ms / segments_;
    step_ms_ = (uint16_t)(step < 1 ? 1 : (step > 200 ? 200 : step));
  }
}

inline bool BootAnim::openSegs() {
  if (segs_) return true;
  segs_ = LittleFS.open(BOOT_SEGS, "r");
  return (bool)segs_;
}

inline void BootAnim::tick(uint32_t now) {
  switch (phase_) {
    case BootPhase::BOOT_IDLE:
      return;

    case BootPhase::BOOT_TEXT:
      // The product name, drawn once and held. Purely a beat before the logo.
      if (now - phase_since_ < 400) return;
      phase_ = BootPhase::BOOT_REVEAL;
      phase_since_ = now;
      last_step_ = now;
      drawn_ = 0;
      if (segs_) { segs_.close(); }
      openSegs();
      return;

    case BootPhase::BOOT_REVEAL:
      if (now - last_step_ < step_ms_) return;
      last_step_ = now;
      drawReveal();
      if (drawn_ >= segments_) {
        phase_ = BootPhase::BOOT_FILL;
        phase_since_ = now;
        drawFill();
      }
      return;

    case BootPhase::BOOT_FILL:
      // One frame is enough: the triangles are already on screen. The delay
      // exists only so the fill reads as a transition rather than a jump.
      if (now - phase_since_ < 250) return;
      phase_ = BootPhase::BOOT_HOLD;
      phase_since_ = now;
      return;

    case BootPhase::BOOT_HOLD:
      if (now - phase_since_ < meta_.hold_ms) return;
      advance(now);
      return;
  }
}

inline void BootAnim::advance(uint32_t now) {
  if (loops_left_ > 1) {
    loops_left_--;
    phase_ = BootPhase::BOOT_TEXT;
    phase_since_ = now;
    drawn_ = 0;
    if (segs_) { segs_.close(); }
    openSegs();
    rr_->invalidate();
    return;
  }
  phase_ = BootPhase::BOOT_IDLE;
  if (segs_) { segs_.close(); }
  if (tris_) { tris_.close(); }
}

// Reads the next segment from the file and draws it. One read per step rather
// than a batch buffer: a single 8-byte read costs the same as an index
// increment, and it removes the refill/exhaustion bookkeeping that a batch
// introduces. At one segment per ~10 ms this is nothing.
inline void BootAnim::drawReveal() {
  if (!openSegs()) { phase_ = BootPhase::BOOT_FILL; return; }
  // File exhausted. Either the reveal is done, or the file is shorter than
  // meta.json claimed; either way the filled logo comes next.
  if (segs_.available() < BOOT_SEG_BYTES) return;
  if (segs_.read((uint8_t*)seg_buf_, BOOT_SEG_BYTES) < BOOT_SEG_BYTES) return;

  const int16_t x1 = seg_buf_[0];
  const int16_t y1 = seg_buf_[1];
  const int16_t x2 = seg_buf_[2];
  const int16_t y2 = seg_buf_[3];
  rr_->drawRawLine(x1, y1, x2, y2, meta_.color);
  drawn_++;
}

inline void BootAnim::drawFill() {
  if (!tris_) {
    tris_ = LittleFS.open(BOOT_TRIS, "r");
    if (!tris_) return;
  }
  // Stream the whole thing, in batches: this is the logo's final state, drawn
  // once, and the renderer's dirty-rect bookkeeping is not worth extending for
  // a frame that happens a single time per boot.
  uint32_t painted = 0;
  while (painted < triangles_) {
    const size_t room = tris_.available();
    if (room == 0) break;
    const size_t want = room > sizeof(tri_buf_) ? sizeof(tri_buf_) : room;
    const int n = tris_.read((uint8_t*)tri_buf_, want);
    if (n < BOOT_TRI_BYTES) break;
    const int count = n / BOOT_TRI_BYTES;
    for (int i = 0; i < count && painted < triangles_; i++, painted++) {
      const int16_t* p = &tri_buf_[i * 6];
      rr_->drawRawTriangle(p[0], p[1], p[2], p[3], p[4], p[5], meta_.color);
    }
  }
}
