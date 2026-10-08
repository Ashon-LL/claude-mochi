// renderer.h — non-blocking, dirty-rect renderer for the ST7789 240x240.
//
// Why not simply full-redraw each frame: a 240x240 RGB565 fill is ~115KB over
// the SPI bus, roughly 25ms at 40MHz. That is the entire animation budget, and
// it starves the BLE stack — which is exactly the failure mode the current
// firmware has, because its transitions call delay() inside the drawing code.
//
// So this renderer watches whether each primitive actually changed and repaints
// only the union of the changed bounding boxes. An idle face costs zero
// redraws; one blinking eye repaints a ~34x66 region. The status bands repaint
// only when they intersect a dirty rect.
//
// Header-only, like the rest of the firmware.
#pragma once
#include <Arduino.h>
#include <Adafruit_GFX.h>
#include "config.h"
#include "expression.h"

// Maximum dirty rectangles in one frame. Raised from 8 to MAX_PRIMS: a face may
// have up to MAX_PRIMS layers and every animated one contributes a rectangle, so
// 8 forced a merge into a single near-screen rect — correct, but it quietly
// turned a mostly-static face into a full repaint for no reason.
#define RECT_MAX MAX_PRIMS

// Colours in this firmware come from rgb565() only, never from bare literals.
// Adafruit_ST77xx.h sets MADCTL_RGB for all four rotations, so the packed value
// is already correct on this panel and no channel swap belongs here. An earlier
// revision of this file added one and it turned every badge and band blue.
//
// Rationale for a local helper rather than the library's own color565(): it is
// declared on the driver subclasses (Adafruit_ST7789), not on Adafruit_GFX, so
// a renderer holding an Adafruit_GFX* cannot reach it. Packing it here keeps
// the caller side identical either way.
inline uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b) {
  return (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
}

struct Box { int16_t x, y, w, h; };

class Renderer {
public:
  void begin(Adafruit_GFX& gfx, uint16_t bg) {
    gfx_ = &gfx;
    bg_  = bg;
    gfx_->setTextWrap(false);
    full_ = true;
    face_ = nullptr;
    nprims_ = 0;
    prevN_ = 0;
    faceChanged_ = false;
    memset(curBox_, 0, sizeof(curBox_));
    memset(prevBox_, 0, sizeof(prevBox_));
    memset(vis_, 0, sizeof(vis_));
    memset(prevVis_, 0, sizeof(prevVis_));
  }

  // nullptr means "no custom face": the renderer then paints only the status
  // bands, and the caller falls back to the compiled-in face for content.
  //
  // A face change does NOT force a full repaint. It used to clear the
  // per-primitive history and set full_, so the next tick cleared the whole
  // screen — a 115 KB SPI burst the panel refreshes row by row, which the user
  // sees as the background sweeping down over the face. That fired on every
  // upload, every bind and every repeated MSG_STATE frame (the daemon's dedupe
  // window is short, so the same state arrives again and again). Instead the
  // change is diffed at the next tick against what is actually on screen —
  // prev_ still holds the last painted state, so only the regions that really
  // differ get repainted; see the faceChanged_ branch in tick(). The one case
  // that genuinely needs full_ is the boot-animation handover, where the screen
  // holds raw pixels no face history describes — the caller invalidates there.
  void setFace(const Expression* f) {
    face_ = f;
    nprims_ = f ? f->nprims : 0;
    faceChanged_ = true;
  }

  // Only a real colour change forces a full repaint: applyFaceForState() runs
  // on every state frame, and an unconditional full_ here turned each no-op
  // into the same row-by-row sweep described above.
  void setBackground(uint16_t c) {
    if (c == bg_) return;
    bg_ = c;
    full_ = true;
  }
  void invalidate() { full_ = true; }

  // ── raw drawing, for the boot animation ──────────────────────
  //
  // These write straight to the panel and register no dirty rect, so nothing
  // will repaint over them until the next invalidate(). That is the intended
  // contract, and it is safe only because of who calls them:
  //
  //   BootAnim draws the logo while it owns the panel, and the caller's loop
  //   does not call tick() with a face during that window — see
  //   boot_anim.h. tick() with no primitives and no dirty rects paints
  //   nothing, so the logo survives. The one thing that does repaint is the
  //   status strip, and it is 16 px tall at the bottom while the logo's data
  //   stays above y=200, so they cannot collide.
  //
  // Anything that needs to survive a face change must be a primitive in an
  // Expression instead. These are not that.
  void drawRawLine(int16_t x1, int16_t y1, int16_t x2, int16_t y2, uint16_t c) {
    if (!gfx_) return;
    gfx_->drawLine(x1, y1, x2, y2, c);
  }

  void drawRawTriangle(int16_t x1, int16_t y1, int16_t x2, int16_t y2,
                       int16_t x3, int16_t y3, uint16_t c) {
    if (!gfx_) return;
    gfx_->fillTriangle(x1, y1, x2, y2, x3, y3, c);
  }

  // The raw colour the background is currently cleared to, so a caller painting
  // over it (the boot animation) can clear with the same value tick() would.
  uint16_t background() const { return bg_; }

  // Drives one frame. Cheap when nothing animates.
  void tick(uint32_t now);

  void setStatus(bool ble, bool host, const char* stateName) {
    const bool nameChanged = !(stateName && stateName_ && strcmp(stateName_, stateName) == 0);
    const bool changed = (ble != ble_) || (host != host_) || nameChanged;
    ble_ = ble; host_ = host;
    if (stateName) { strncpy(stateName_, stateName, 15); stateName_[15] = '\0'; }
    else stateName_[0] = '\0';
    if (changed) bandDirty_ = true;
  }

  // Test seam: how many frames actually repainted something. Used by the
  // self-check on boot so a wedged renderer is visible without a debugger.
  uint32_t paintedFrames() const { return paintedFrames_; }

private:
  // ── geometry ────────────────────────────────────────────────
  static bool intersects(const Box& a, const Box& b) {
    const int32_t ax2 = (int32_t)a.x + a.w, bx2 = (int32_t)b.x + b.w;
    const int32_t ay2 = (int32_t)a.y + a.h, by2 = (int32_t)b.y + b.h;
    return !(ax2 <= b.x || bx2 <= a.x || ay2 <= b.y || by2 <= a.y);
  }
  static void mergeInto(Box& a, const Box& b) {
    const int32_t x1 = a.x < b.x ? a.x : b.x;
    const int32_t y1 = a.y < b.y ? a.y : b.y;
    const int32_t x2 = (int32_t)(a.x + a.w) > (int32_t)(b.x + b.w) ? (int32_t)(a.x + a.w) : (int32_t)(b.x + b.w);
    const int32_t y2 = (int32_t)(a.y + a.h) > (int32_t)(b.y + b.h) ? (int32_t)(a.y + b.h) : (int32_t)(b.y + b.h);
    a.x = (int16_t)x1; a.y = (int16_t)y1;
    a.w = (int16_t)(x2 - x1); a.h = (int16_t)(y2 - y1);
  }
  void addRect(int16_t x, int16_t y, int16_t w, int16_t h) {
    if (w <= 0 || h <= 0) return;
    if (nDirty_ >= RECT_MAX) {
      for (uint8_t i = 1; i < nDirty_; i++) mergeInto(dirty_[0], dirty_[i]);
      nDirty_ = 1;
    }
    Box r{ x, y, w, h };
    for (uint8_t i = 0; i < nDirty_; i++) {
      if (intersects(dirty_[i], r)) { mergeInto(dirty_[i], r); return; }
    }
    if (nDirty_ < RECT_MAX) dirty_[nDirty_++] = r;
  }

  // Returns whether the primitive is visible this frame. Kept separate from
  // the colour because FX_BLINK's "off" phase means "not drawn", not "drawn
  // in the background colour" — the latter would erase neighbouring content.
  bool resolvePrim_(Prim& p, uint32_t now);
  Box  boundsOf_(const Prim& p) const;
  void drawPrim_(const Prim& p);

  // Status bands: top-right BLE indicator, bottom host/state strip.
  void drawBands_();
  void drawBadge_();
  void drawStrip_();
  Box badgeBox_() const { return Box{ (int16_t)(DISP_W - 46), 0, 46, BADGE_H }; }
  Box stripBox_() const { return Box{ 0, (int16_t)(DISP_H - STATUS_H), DISP_W, STATUS_H }; }

  int16_t textW_(const Prim& p) const { return (int16_t)(strlen(p.text) * 6 * p.size); }

  Adafruit_GFX* gfx_ = nullptr;
  uint16_t     bg_   = 0;

  const Expression* face_ = nullptr;
  uint8_t  nprims_ = 0;
  uint8_t  prevN_  = 0;   // prim count as of the last painted frame
  bool     faceChanged_ = false;  // setFace() since the last tick: diff, don't clear
  Prim     cur_[MAX_PRIMS];
  Prim     prev_[MAX_PRIMS];
  Box      curBox_[MAX_PRIMS];
  Box      prevBox_[MAX_PRIMS];
  bool     vis_[MAX_PRIMS];         // visible this frame, per prim
  bool     prevVis_[MAX_PRIMS];     // visible last frame, per prim

  Box      dirty_[RECT_MAX];
  uint8_t  nDirty_    = 0;
  bool     full_      = true;
  bool     bandDirty_ = false;

  bool      ble_  = false;
  bool      host_ = false;
  char      stateName_[16] = "";
  uint32_t  paintedFrames_ = 0;
};

// ── inline: colour blending for FX_FADE ───────────────────────
// Operates in panel space (BGR), because both operands are already converted.
inline uint16_t blend565(uint16_t a, uint16_t b, uint8_t t) {
  const int32_t ar = (a >> 11) & 0x1F, ag = (a >> 5) & 0x3F, ab = a & 0x1F;
  const int32_t br = (b >> 11) & 0x1F, bg = (b >> 5) & 0x3F, bb = b & 0x1F;
  const int32_t u = 255 - t;
  const int32_t r = (ar * u + br * t) / 255;
  const int32_t g = (ag * u + bg * t) / 255;
  const int32_t c = (ab * u + bb * t) / 255;
  return (uint16_t)((r << 11) | (g << 5) | c);
}

// ── per-prim animation ────────────────────────────────────────
inline bool Renderer::resolvePrim_(Prim& p, uint32_t now) {
  if (p.fx == FX_NONE || p.period_ms == 0) return true;

  const uint32_t period = p.period_ms;
  const uint32_t phase  = now % period;

  switch (p.fx) {
    case FX_BLINK:
      return phase < p.on_ms;                 // hidden for the rest of the period

    case FX_PULSE: {
      // Triangle wave, integer only: rises then falls, so it stays in bounds.
      uint32_t tri = phase * 2;
      if (tri > period) tri = period * 2 - tri;
      const int32_t k = 100 + ((int32_t)(p.amount - 100) * (int32_t)tri) / (int32_t)period;
      if (p.type == PRIM_RECT) {
        const int32_t cx = p.x + p.x2 / 2, cy = p.y + p.y2 / 2;
        p.x2 = (int16_t)(((int32_t)p.x2 * k) / 100);
        p.y2 = (int16_t)(((int32_t)p.y2 * k) / 100);
        p.x  = (int16_t)(cx - p.x2 / 2);
        p.y  = (int16_t)(cy - p.y2 / 2);
      } else if (p.type == PRIM_CIRCLE) {
        p.x2 = (int16_t)(((int32_t)p.x2 * k) / 100);   // radius only; centre fixed
      }
      return true;
    }

    case FX_SHAKE: {
      // A gaze sweep with dwell at each end, not a vibration.
      //
      // The old version flipped ±amount every 90 ms and never read period_ms,
      // so the cycle slider in the editor did nothing while the layer vibrated
      // at a fixed 90 ms — which is what "一直在急速闪烁" was. "Look left, then
      // right" wants to arrive somewhere and stay, so most of the cycle is
      // spent parked at each end and only the middle travels.
      //
      // The breakpoints are duplicated in the host preview
      // (renderExpression.ts). If they drift, the preview and the panel
      // disagree, which is the whole failure mode this pair exists to avoid.
      const float t = (float)phase / (float)period;
      float s;
      if (t < 0.30f)      s = -1.0f;                                     // look left
      else if (t < 0.50f) s = -1.0f + (2.0f * (t - 0.30f)) / 0.20f;      // travel
      else if (t < 0.80f) s =  1.0f;                                     // look right
      else                s =  1.0f - (2.0f * (t - 0.80f)) / 0.20f;      // travel back
      p.x = (int16_t)(p.x + s * p.amount);
      return true;
    }

    case FX_SPIN: {
      const float a = ((float)((uint64_t)now * (uint32_t)p.amount) / (float)period) * DEG_TO_RAD;
      const float c = cosf(a), s = sinf(a);
      if (p.type == PRIM_LINE) {
        const float cx = (p.x + p.x2) * 0.5f, cy = (p.y + p.y2) * 0.5f;
        const float dx1 = p.x - cx,  dy1 = p.y - cy;
        const float dx2 = p.x2 - cx, dy2 = p.y2 - cy;
        p.x  = (int16_t)(cx + dx1 * c - dy1 * s);
        p.y  = (int16_t)(cy + dx1 * s + dy1 * c);
        p.x2 = (int16_t)(cx + dx2 * c - dy2 * s);
        p.y2 = (int16_t)(cy + dx2 * s + dy2 * c);
      } else if (p.type == PRIM_POLY) {
        int32_t cx = 0, cy = 0;
        for (uint8_t i = 0; i < p.npts; i++) { cx += p.pts[i * 2]; cy += p.pts[i * 2 + 1]; }
        cx /= p.npts; cy /= p.npts;
        for (uint8_t i = 0; i < p.npts; i++) {
          const float dx = (float)(p.pts[i * 2] - cx), dy = (float)(p.pts[i * 2 + 1] - cy);
          p.pts[i * 2    ] = (int16_t)(cx + dx * c - dy * s);
          p.pts[i * 2 + 1] = (int16_t)(cy + dx * s + dy * c);
        }
      }
      return true;
    }

    case FX_FADE: {
      p.color = blend565(p.color, bg_, (uint8_t)(((uint32_t)p.amount * 255) / 100));
      return true;
    }

    default: return true;
  }
}

// ── bounding boxes ────────────────────────────────────────────
inline Box Renderer::boundsOf_(const Prim& p) const {
  Box b{ 0, 0, 0, 0 };
  switch (p.type) {
    case PRIM_RECT:   b.x = p.x; b.y = p.y; b.w = p.x2; b.h = p.y2; break;
    case PRIM_CIRCLE:
      b.x = (int16_t)(p.x - p.x2); b.y = (int16_t)(p.y - p.x2);
      b.w = (int16_t)(p.x2 * 2);   b.h = (int16_t)(p.x2 * 2);
      break;
    case PRIM_LINE: {
      const int16_t x1 = p.x < p.x2 ? p.x : p.x2, x2 = p.x > p.x2 ? p.x : p.x2;
      const int16_t y1 = p.y < p.y2 ? p.y : p.y2, y2 = p.y > p.y2 ? p.y : p.y2;
      b.x = x1; b.y = y1; b.w = (int16_t)(x2 - x1 + 1); b.h = (int16_t)(y2 - y1 + 1);
      break;
    }
    case PRIM_POLY: {
      int16_t x1 = 32767, y1 = 32767, x2 = -32767, y2 = -32767;
      for (uint8_t i = 0; i < p.npts; i++) {
        if (p.pts[i * 2]     < x1) x1 = p.pts[i * 2];
        if (p.pts[i * 2]     > x2) x2 = p.pts[i * 2];
        if (p.pts[i * 2 + 1] < y1) y1 = p.pts[i * 2 + 1];
        if (p.pts[i * 2 + 1] > y2) y2 = p.pts[i * 2 + 1];
      }
      b.x = x1; b.y = y1; b.w = (int16_t)(x2 - x1 + 1); b.h = (int16_t)(y2 - y1 + 1);
      break;
    }
    case PRIM_TEXT:
      b.x = p.x; b.y = p.y; b.w = textW_(p); b.h = (int16_t)(8 * p.size);
      break;
    default: break;
  }
  return b;
}

// ── drawing one primitive ─────────────────────────────────────
inline void Renderer::drawPrim_(const Prim& p) {
  switch (p.type) {
    case PRIM_RECT:   gfx_->fillRect(p.x, p.y, p.x2, p.y2, p.color);        break;
    case PRIM_CIRCLE: gfx_->fillCircle(p.x, p.y, p.x2, p.color);           break;
    case PRIM_LINE:   gfx_->drawLine(p.x, p.y, p.x2, p.y2, p.color);       break;
    case PRIM_TEXT:
      gfx_->setTextColor(p.color);
      gfx_->setTextSize(p.size);
      gfx_->setCursor(p.x, p.y);
      gfx_->print(p.text);
      break;
    // Polygons render as outlines in v1: Adafruit_GFX ships no fillPolygon,
    // and a hand-rolled scanline fill is not worth the RAM on the C3. Faces
    // use polys for chevrons and corners, where an outline is what we want.
    case PRIM_POLY:
      for (uint8_t i = 0; i < p.npts; i++) {
        const uint8_t j = (uint8_t)((i + 1) % p.npts);
        gfx_->drawLine(p.pts[i * 2], p.pts[i * 2 + 1], p.pts[j * 2], p.pts[j * 2 + 1], p.color);
      }
      break;
    default: break;
  }
}

// ── status bands ──────────────────────────────────────────────
inline void Renderer::drawBadge_() {
  const Box b = badgeBox_();
  gfx_->fillRect(b.x, b.y, b.w, b.h, bg_);

  // Three signal bars, filled when the link is up; hollow red when down. The
  // original complaint was "after a reboot I cannot tell whether BLE is
  // connected", so this has to be readable at a glance without any tooling.
  const uint16_t col = ble_ ? rgb565(80, 220, 130)
                            : rgb565(255, 80, 80);
  for (uint8_t i = 0; i < 3; i++) {
    const int16_t h = (int16_t)(i * 4 + 3);
    const int16_t x = (int16_t)(b.x + b.w - 4 - i * 5);
    const int16_t y = (int16_t)(b.y + b.h - 2 - h);
    if (ble_) gfx_->fillRect(x, y, 3, h, col);
    else      gfx_->drawRect(x, y, 3, h, col);
  }
}

inline void Renderer::drawBands_() { drawBadge_(); drawStrip_(); }

inline void Renderer::drawStrip_() {
  const Box b = stripBox_();
  gfx_->fillRect(b.x, b.y, b.w, b.h, bg_);
  gfx_->setTextSize(1);

  gfx_->setTextColor(host_ ? rgb565(255, 220, 80)
                           : rgb565(255, 80, 80));
  gfx_->setCursor(2, b.y + 4);
  gfx_->print(host_ ? "PC:ON " : "PC:OFF");

  char label[16];
  strncpy(label, stateName_, 15); label[15] = '\0';
  gfx_->setTextColor(rgb565(255, 255, 255));
  const int16_t w = (int16_t)(strlen(label) * 6);
  gfx_->setCursor((int16_t)(DISP_W - 2 - w), b.y + 4);
  gfx_->print(label);
}

// ── the frame tick ────────────────────────────────────────────
//
// The rule this implements, in one sentence: repaint the union of every region
// whose content can differ from what is already on screen, and nothing more.
//
// The set of regions is not inferred by diffing colours and boxes and guessing
// what changed. An earlier version did exactly that, and it had a hole it could
// not express: a primitive that becomes visible or invisible has the *same* box
// and the *same* colour either way, so the diff saw nothing change and never
// marked its region — while the animations around it cleared that same region.
// What was left was a hole nothing repainted, which on a face with a blink layer
// over an eye reads as the eye flickering and being cut into blocks.
//
// So the sets are enumerated instead of deduced:
//
//   * every *animating* primitive contributes its old and new box — but only on
//     frames where it moved, changed colour, or flipped. Most frames nothing
//     has: a blink lid between blinks is a static primitive by any other name,
//     and repainting it anyway wipes a lid-sized region 30 times a second,
//     which on a lid over an eye reads as that area shimmering.
//   * when a visibility flips, the flipped primitive's box together with every
//     box it meets. Those are the only primitives whose appearance can have
//     changed, because the flipped one now covers them or no longer does. This
//     is a local patch, not a screen clear: a full-screen clear is a 115 KB SPI
//     burst the panel refreshes row by row, which the user sees as the
//     background sweeping down over the face.
//   * a face change diffs the new face against the last painted frame and
//     repaints only the boxes that differ — added prims, removed prims, and
//     prims whose resolved state changed. A whole-screen clear on every face
//     switch was the original rule; it was correct but it swept the background
//     down over the face on every state frame, which is exactly the artifact
//     this renderer exists to avoid. A static primitive *drifting* — geometry
//     changing with no face change behind it — still repaints everything,
//     because partial reasoning cannot say what else moved.
//
// Layer order still decides who covers whom: within one repaint, all primitives
// meeting the region are drawn in list order, so an eyelid listed after an eye
// covers it, and a pupil listed before an eyelid is covered by it. That is why
// the editor appends synthesised lids at the end of the list.
inline void Renderer::tick(uint32_t now) {
  if (!gfx_) return;

  // 1. resolve every primitive at this instant, fresh from the face each time
  //    so phase-driven mutation never accumulates.
  for (uint8_t i = 0; i < nprims_; i++) cur_[i] = face_->prims[i];
  for (uint8_t i = 0; i < nprims_; i++) vis_[i] = resolvePrim_(cur_[i], now);
  for (uint8_t i = 0; i < nprims_; i++) curBox_[i] = boundsOf_(cur_[i]);

  // 2. decide the extent of the repaint
  nDirty_ = 0;
  bool flip = false, moved = false;
  for (uint8_t i = 0; i < nprims_; i++) {
    if (vis_[i] != prevVis_[i]) flip = true;
    // Only a primitive that does not animate itself is checked for geometry
    // drift: an animating one changing shape is the normal case, and
    // full-repainting for it would make every frame cost a whole screen.
    if (cur_[i].fx == FX_NONE &&
        (cur_[i].color != prev_[i].color ||
         curBox_[i].x != prevBox_[i].x || curBox_[i].y != prevBox_[i].y ||
         curBox_[i].w != prevBox_[i].w || curBox_[i].h != prevBox_[i].h)) {
      moved = true;
    }
  }

  // A visibility flip is the one change a box diff cannot see: a primitive that
  // becomes visible or invisible has the same box either way. It is handled by
  // repainting the flipped primitive together with everything it meets — those
  // are the only primitives whose appearance can have changed, since a
  // primitive now covers them or no longer does.
  //
  // It deliberately does NOT repaint the whole screen. A full-screen clear is a
  // 115 KB SPI burst that the ST7789 refreshes row by row, so the user sees the
  // background colour sweep down over the face twice per blink — reported as
  // "像流线一样从上往下捋一遍", and correctly so.
  if (flip && !full_) {
    for (uint8_t i = 0; i < nprims_; i++) {
      if (vis_[i] == prevVis_[i]) continue;
      for (uint8_t j = 0; j < nprims_; j++) {
        if (!intersects(curBox_[j], curBox_[i])) continue;
        Box u = prevBox_[j];
        mergeInto(u, curBox_[j]);
        addRect(u.x, u.y, u.w, u.h);
      }
    }
  }

  if (full_) {
    // An explicit invalidate() or a background change. Whole screen.
    full_ = false;
    faceChanged_ = false;
    dirty_[0] = Box{ 0, 0, DISP_W, DISP_H };
    nDirty_ = 1;
  } else if (faceChanged_) {
    // A face change, diffed against what is on screen. prev_ holds the last
    // painted (already resolved) state, so per index there are three cases:
    // exists on both sides and differs → repaint the union of the two boxes;
    // added by the new face → paint its box; removed → repaint its old box so
    // the region clear gives it back to the background. A visibility flip is
    // not handled here — the pass above already owns it, and at a superset of
    // this branch's coverage.
    //
    // memcmp is valid because every Prim is memset before its fields are
    // written (addPrim, parseExpression), so padding bytes are 0 on both sides.
    // What is compared is resolved state against resolved state — exactly "what
    // is on screen" versus "what will be", which is the repaint rule in the
    // header comment. The full-screen clear this replaces is what the user saw
    // as a sweep on every upload and repeated state frame.
    faceChanged_ = false;
    const uint8_t n = nprims_ > prevN_ ? nprims_ : prevN_;
    for (uint8_t i = 0; i < n; i++) {
      if (i < nprims_ && i < prevN_) {
        if (vis_[i] != prevVis_[i]) continue;
        if (memcmp(&cur_[i], &prev_[i], sizeof(Prim)) == 0) continue;
        Box u = prevBox_[i];
        mergeInto(u, curBox_[i]);
        addRect(u.x, u.y, u.w, u.h);
      } else if (i < nprims_) {
        const Box& b = curBox_[i];
        addRect(b.x, b.y, b.w, b.h);
      } else {
        const Box& b = prevBox_[i];
        addRect(b.x, b.y, b.w, b.h);
      }
    }
  } else if (moved) {
    // A static primitive drifting. Whole screen: rare, and the only case where
    // partial reasoning is known to be unsafe.
    dirty_[0] = Box{ 0, 0, DISP_W, DISP_H };
    nDirty_ = 1;
  } else {
    for (uint8_t i = 0; i < nprims_; i++) {
      if (cur_[i].fx == FX_NONE) continue;   // a static primitive cannot change
      // A frame in which this primitive did not change costs nothing. A blink
      // lid at rest is bit-for-bit identical to a static primitive, yet the old
      // rule redrew it every frame: 30 wipes a second of a region the size of
      // the lid, which on a lid covering an eye reads as that area shimmering.
      // The pixels were always right; only the row-by-row SPI refresh showed.
      //
      // Visibility flips are excluded here because the branch above already
      // owns them — together with every box the flipped lid meets — and that
      // coverage is exactly the diffing this loop replaced.
      if (vis_[i] != prevVis_[i]) continue;
      if (cur_[i].color == prev_[i].color &&
          curBox_[i].x  == prevBox_[i].x  && curBox_[i].y == prevBox_[i].y &&
          curBox_[i].w  == prevBox_[i].w  && curBox_[i].h == prevBox_[i].h) continue;
      Box u = prevBox_[i];
      mergeInto(u, curBox_[i]);
      addRect(u.x, u.y, u.w, u.h);
    }
    if (bandDirty_) {
      bandDirty_ = false;
      const Box badge = badgeBox_();
      const Box strip = stripBox_();
      addRect(badge.x, badge.y, badge.w, badge.h);
      addRect(strip.x, strip.y, strip.w, strip.h);
    }
  }

  // 3. repaint. Clear the region, then draw every visible primitive that meets
  //    it, in layer order. Drawing the whole primitive rather than clipping it to
  //    the region is intentional and safe: a primitive that extends past the
  //    region leaves its other half untouched, and the clear only covers the
  //    region, so re-drawing what was already there changes nothing.
  for (uint8_t r = 0; r < nDirty_; r++) {
    const Box& d = dirty_[r];
    gfx_->fillRect(d.x, d.y, d.w, d.h, bg_);
    for (uint8_t i = 0; i < nprims_; i++) {
      if (vis_[i] && intersects(curBox_[i], d)) drawPrim_(cur_[i]);
    }
    if (intersects(badgeBox_(), d)) drawBadge_();
    if (intersects(stripBox_(), d)) drawStrip_();
  }

  if (nDirty_ > 0) paintedFrames_++;
  memcpy(prev_, cur_, sizeof(prev_));
  memcpy(prevBox_, curBox_, sizeof(prevBox_));
  memcpy(prevVis_, vis_, sizeof(prevVis_));
  prevN_ = nprims_;
}
