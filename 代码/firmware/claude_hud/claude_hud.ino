/*
 * Claude HUD — ESP32-C3 SuperMini + ST7789 240x240, BLE status display.
 *
 * Boot order is deliberate: the panel lights up first so the user sees
 * something within ~1s of power-on, then storage, then BLE. The status bands
 * report each stage as it completes, which is the whole point of this rewrite —
 * the original firmware only printed link state to the serial port.
 *
 * Everything below setup() is non-blocking. If you reach for delay() down
 * there, the renderer will silently eat the BLE budget and the HUD will lag.
 *
 * Wiring per D:\Claude DIY\接线.xlsx. Pins are in config.h.
 */
#include "config.h"
#include "protocol.h"
#include "expression.h"
#include "store.h"
#include "state_machine.h"
#include "renderer.h"
#include "boot_anim.h"
#include "boot_data.h"
#include "ble_service.h"

#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>

Adafruit_ST7789 tft = Adafruit_ST7789(PIN_TFT_CS, PIN_TFT_DC, PIN_TFT_RST);

#define BG_R 218
#define BG_G 17
#define BG_B 0

// Colours go through rgb565() from renderer.h: the packed value is used as-is
// on this panel (the ST7789 driver sets MADCTL_RGB), so no conversion belongs
// here and the faces must not invent their own packing.
#define faceCol(r, g, b) rgb565(r, g, b)

// Kept as a variable rather than recomputed: loop() needs the same value setup()
// cleared the panel with, and two places computing a colour is how the bands end
// up one shade off the background.
static uint16_t bgColor = faceCol(BG_R, BG_G, BG_B);

// True once the BLE stack is running. The start-up animation delays it on
// purpose (see setup()), so it cannot simply be done inline.
static bool bleStarted_ = false;

static Store        store;
static StateMachine sm;
static Renderer     rr;
static BootAnim     boot;
static BleService   ble;

// Captured once so a later storage failure does not silently blank the HUD.
static bool storeOk = false;

// ── Faces ─────────────────────────────────────────────────────
// Built programmatically instead of as nested initialisers: Prim has 14 fields,
// and a mistake in an aggregate initialiser yields a dark screen with no
// compiler complaint. addPrim() zeroes every unused field for us.
static Expression faceIdle, faceThinking, faceToolStart, faceToolEnd, faceWaiting,
                  faceError, faceOffline;
static Expression* faces[ST_COUNT];

// Runtime-loaded expressions, one slot per state. Kept separate from the
// upload buffer so a download in progress cannot clobber what is on screen.
static Expression custom[ST_COUNT];
static bool       haveCustom[ST_COUNT];
static uint8_t    customBuf[EXPR_MAX_BYTES];

// Expression upload state machine: BEGIN -> CHUNK* -> COMMIT.
static uint8_t  exprSlot = 0, exprType = 0, exprSeq = 0;
static uint16_t exprLen = 0, exprGot = 0, exprCrc = 0;
static uint8_t  exprBuf[EXPR_MAX_BYTES];
static bool     exprActive = false;

// Boot-file upload, the same three phases for a different destination.
// which is BOOT_TARGET_*, defined in config.h. Separate state from the
// expression upload on purpose: interleaving them would make an aborted
// expression upload silently corrupt a boot file, or the reverse.
static uint8_t  bootWhich = 0, bootType = 0, bootSeq = 0;
static uint16_t bootLen = 0, bootGot = 0, bootCrc = 0;
static uint8_t  bootBuf[BOOT_MAX_BYTES];
static bool     bootActive = false;

static uint8_t  lastErr = ACK_OK;

// ── Boot self-check ───────────────────────────────────────────
static bool     selfCheckDone = false;
static uint8_t  selfCheckFail = 0;

// ── Button (GPIO9 / BOOT) ─────────────────────────────────────
static bool     btnLast = HIGH;
static uint32_t btnDownAt = 0;
static bool     btnLongFired = false;
static uint8_t  previewIdx = 0;
static uint32_t previewUntil = 0;   // 0 = no preview active
#define PREVIEW_MS 8000UL

// =====================================================================
// Faces
// =====================================================================
static uint8_t addPrim(Expression& e, PrimType t) {
  if (e.nprims >= MAX_PRIMS) return 0xFF;
  Prim& p = e.prims[e.nprims++];
  memset(&p, 0, sizeof(p));
  p.type = t;
  p.amount = 100;
  return (uint8_t)(e.nprims - 1);
}

static void buildFaces() {
  // ── IDLE: open eyes + smile ──────────────────────────────────
  {
    Expression& e = faceIdle;
    uint8_t l = addPrim(e, PRIM_RECT);
    e.prims[l].color = faceCol(0, 0, 0); e.prims[l].x = 62;  e.prims[l].y = 70;
    e.prims[l].x2 = 28;       e.prims[l].y2 = 46;
    uint8_t r = addPrim(e, PRIM_RECT);
    e.prims[r].color = faceCol(0, 0, 0); e.prims[r].x = 150; e.prims[r].y = 70;
    e.prims[r].x2 = 28;       e.prims[r].y2 = 46;

    uint8_t m = addPrim(e, PRIM_POLY);
    e.prims[m].color = faceCol(0, 0, 0);
    e.prims[m].npts = 3;
    e.prims[m].pts[0] = 96;  e.prims[m].pts[1] = 156;
    e.prims[m].pts[2] = 120; e.prims[m].pts[3] = 166;
    e.prims[m].pts[4] = 144; e.prims[m].pts[5] = 156;
  }

  // ── THINKING: one open eye, one squinted, drifting thought dots ──
  {
    Expression& e = faceThinking;
    uint8_t l = addPrim(e, PRIM_RECT);
    e.prims[l].color = faceCol(0, 0, 0); e.prims[l].x = 62;  e.prims[l].y = 70;
    e.prims[l].x2 = 28;       e.prims[l].y2 = 46;
    uint8_t r = addPrim(e, PRIM_RECT);
    e.prims[r].color = faceCol(0, 0, 0); e.prims[r].x = 150; e.prims[r].y = 88;
    e.prims[r].x2 = 28;       e.prims[r].y2 = 6;

    // Chase pattern at 1500ms: only one circle is lit at a time, so the
    // dirty region is a single 10px circle rather than three.
    for (uint8_t i = 0; i < 3; i++) {
      uint8_t c = addPrim(e, PRIM_CIRCLE);
      e.prims[c].color = faceCol(90, 88, 86);
      e.prims[c].x = 186 + i * 12; e.prims[c].y = 30;
      e.prims[c].x2 = 3 + i;      // radius grows with distance
      e.prims[c].fx = FX_BLINK;
      e.prims[c].period_ms = 1500;
      e.prims[c].on_ms = 500;
      e.prims[c].animating = true;
    }
  }

  // ── TOOL_START: chevron eyes + hammer ────────────────────────
  {
    Expression& e = faceToolStart;
    // A chevron is two segments; closing the loop back to the start makes the
    // second sweep visible, which is the point of the ">" shape.
    uint8_t l = addPrim(e, PRIM_POLY);
    e.prims[l].color = faceCol(0, 0, 0);
    e.prims[l].npts = 4;
    e.prims[l].pts[0] = 62;  e.prims[l].pts[1] = 66;
    e.prims[l].pts[2] = 90;  e.prims[l].pts[3] = 93;
    e.prims[l].pts[4] = 62;  e.prims[l].pts[5] = 120;
    e.prims[l].pts[6] = 62;  e.prims[l].pts[7] = 66;

    uint8_t r = addPrim(e, PRIM_POLY);
    e.prims[r].color = faceCol(0, 0, 0);
    e.prims[r].npts = 4;
    e.prims[r].pts[0] = 178; e.prims[r].pts[1] = 66;
    e.prims[r].pts[2] = 150; e.prims[r].pts[3] = 93;
    e.prims[r].pts[4] = 178; e.prims[r].pts[5] = 120;
    e.prims[r].pts[6] = 178; e.prims[r].pts[7] = 66;

    uint8_t h = addPrim(e, PRIM_RECT);
    e.prims[h].color = faceCol(90, 88, 86); e.prims[h].x = 198; e.prims[h].y = 40;
    e.prims[h].x2 = 4;        e.prims[h].y2 = 26;
    uint8_t hd = addPrim(e, PRIM_RECT);
    e.prims[hd].color = faceCol(90, 88, 86); e.prims[hd].x = 188; e.prims[hd].y = 34;
    e.prims[hd].x2 = 24;       e.prims[hd].y2 = 9;
    e.prims[hd].fx = FX_PULSE;
    e.prims[hd].period_ms = 900;
    e.prims[hd].amount = 140;
    e.prims[hd].animating = true;
  }

  // ── TOOL_END: open eyes + green check ────────────────────────
  {
    Expression& e = faceToolEnd;
    uint8_t l = addPrim(e, PRIM_RECT);
    e.prims[l].color = faceCol(0, 0, 0); e.prims[l].x = 62;  e.prims[l].y = 70;
    e.prims[l].x2 = 28;       e.prims[l].y2 = 46;
    uint8_t r = addPrim(e, PRIM_RECT);
    e.prims[r].color = faceCol(0, 0, 0); e.prims[r].x = 150; e.prims[r].y = 70;
    e.prims[r].x2 = 28;       e.prims[r].y2 = 46;

    uint8_t c = addPrim(e, PRIM_POLY);
    e.prims[c].color = faceCol(80, 220, 130);
    e.prims[c].npts = 4;
    e.prims[c].pts[0] = 100; e.prims[c].pts[1] = 156;
    e.prims[c].pts[2] = 108; e.prims[c].pts[3] = 164;
    e.prims[c].pts[4] = 144; e.prims[c].pts[5] = 140;
    e.prims[c].pts[6] = 162; e.prims[c].pts[7] = 152;
  }

  // ── WAITING: closed eyes + a spinning diameter ───────────────
  {
    Expression& e = faceWaiting;
    uint8_t l = addPrim(e, PRIM_RECT);
    e.prims[l].color = faceCol(0, 0, 0); e.prims[l].x = 62;  e.prims[l].y = 88;
    e.prims[l].x2 = 28;       e.prims[l].y2 = 6;
    uint8_t r = addPrim(e, PRIM_RECT);
    e.prims[r].color = faceCol(0, 0, 0); e.prims[r].x = 150; e.prims[r].y = 88;
    e.prims[r].x2 = 28;       e.prims[r].y2 = 6;

    // One rotating diameter reads as rotation at this radius; four lines
    // would cost 4x the SPI traffic for the same impression.
    uint8_t s = addPrim(e, PRIM_LINE);
    e.prims[s].color = faceCol(80, 160, 255); e.prims[s].x = 120; e.prims[s].y = 160;
    e.prims[s].x2 = 140;       e.prims[s].y2 = 180;
    e.prims[s].fx = FX_SPIN;
    e.prims[s].period_ms = 1200;
    e.prims[s].amount = 360;   // one full turn per period
    e.prims[s].animating = true;
  }

  // ── ERROR: crossed eyes ──────────────────────────────────────
  {
    Expression& e = faceError;
    uint8_t a = addPrim(e, PRIM_LINE);
    e.prims[a].color = faceCol(255, 80, 80); e.prims[a].x = 60;  e.prims[a].y = 66;
    e.prims[a].x2 = 92;        e.prims[a].y2 = 118;
    uint8_t b = addPrim(e, PRIM_LINE);
    e.prims[b].color = faceCol(255, 80, 80); e.prims[b].x = 92;  e.prims[b].y = 66;
    e.prims[b].x2 = 60;        e.prims[b].y2 = 118;
    uint8_t c = addPrim(e, PRIM_LINE);
    e.prims[c].color = faceCol(255, 80, 80); e.prims[c].x = 148; e.prims[c].y = 66;
    e.prims[c].x2 = 180;       e.prims[c].y2 = 118;
    uint8_t d = addPrim(e, PRIM_LINE);
    e.prims[d].color = faceCol(255, 80, 80); e.prims[d].x = 180; e.prims[d].y = 66;
    e.prims[d].x2 = 148;       e.prims[d].y2 = 118;

    uint8_t t = addPrim(e, PRIM_TEXT);
    e.prims[t].color = faceCol(255, 80, 80); e.prims[t].x = 70; e.prims[t].y = 140;
    e.prims[t].size = 2;
    strncpy(e.prims[t].text, "ERROR", TEXT_MAX_LEN - 1);
  }

  // ── OFFLINE: flat eyes ───────────────────────────────────────
  {
    Expression& e = faceOffline;
    uint8_t l = addPrim(e, PRIM_RECT);
    e.prims[l].color = faceCol(0, 0, 0); e.prims[l].x = 62;  e.prims[l].y = 88;
    e.prims[l].x2 = 28;       e.prims[l].y2 = 5;
    uint8_t r = addPrim(e, PRIM_RECT);
    e.prims[r].color = faceCol(0, 0, 0); e.prims[r].x = 150; e.prims[r].y = 88;
    e.prims[r].x2 = 28;       e.prims[r].y2 = 5;

    uint8_t t = addPrim(e, PRIM_TEXT);
    e.prims[t].color = faceCol(255, 255, 255); e.prims[t].x = 58; e.prims[t].y = 118;
    e.prims[t].size = 2;
    strncpy(e.prims[t].text, "WAITING FOR PC", TEXT_MAX_LEN - 1);
  }

  faces[ST_IDLE]       = &faceIdle;
  faces[ST_THINKING]   = &faceThinking;
  faces[ST_TOOL_START] = &faceToolStart;
  faces[ST_TOOL_END]   = &faceToolEnd;
  faces[ST_WAITING]    = &faceWaiting;
  faces[ST_ERROR]      = &faceError;
  faces[ST_OFFLINE]    = &faceOffline;

  // Every built-in defaults to the panel's own background. set in one loop
  // rather than per-face because the alternative is seven places to forget,
  // and an uninitialised bg would clear the screen to a garbage colour.
  for (uint8_t i = 0; i < ST_COUNT; i++) faces[i]->bg = bgColor;
}

// =====================================================================
// Custom expressions
// =====================================================================
// Every failure path here used to be a silent return, which meant "my custom
// face does not show up" had no answer anywhere — not in the daemon log, not on
// the panel. Each path now names itself over MSG_LOG so the failure is visible
// on the host instead of being inferred.
static void loadCustomForState(uint8_t st) {
  haveCustom[st] = false;
  if (st >= ST_COUNT) return;

  const uint8_t slot = store.stateSlot(st);
  char why[48];

  if (slot == 0xFF) {
    snprintf(why, sizeof(why), "slot: state %u unbound", st);
    ble.send(MSG_LOG, 0, (const uint8_t*)why, (uint16_t)strlen(why));
    return;
  }
  if (slot >= SLOT_COUNT || !store.slotPresent(slot)) {
    snprintf(why, sizeof(why), "slot: %u empty or out of range", slot);
    ble.send(MSG_LOG, 0, (const uint8_t*)why, (uint16_t)strlen(why));
    return;
  }

  uint16_t n = 0;
  if (!store.readSlot(slot, customBuf, sizeof(customBuf), n)) {
    snprintf(why, sizeof(why), "slot: read failed on %u", slot);
    ble.send(MSG_LOG, 0, (const uint8_t*)why, (uint16_t)strlen(why));
    return;
  }
  const char* reason = "unknown";
  // The panel's current background is the fallback for a face that has no "bg"
  // of its own, so an older expression keeps the background it was made for.
  if (!parseExpression(customBuf, n, custom[st], bgColor, &reason)) {
    snprintf(why, sizeof(why), "slot: %u rejected (%u bytes): %s", slot, n, reason);
    ble.send(MSG_LOG, 0, (const uint8_t*)why, (uint16_t)strlen(why));
    return;
  }

  haveCustom[st] = true;
  // Report how many layers actually ended up animated. "The face uploaded but
  // nothing moves" is otherwise indistinguishable from "the animation is too
  // subtle to see" — and it was the effect field being read as an integer, so
  // every face arrived with its animation stripped. This line is what tells the
  // two apart from the host side.
  {
    uint8_t moving = 0;
    for (uint8_t i = 0; i < custom[st].nprims; i++) {
      if (custom[st].prims[i].fx != FX_NONE &&
          custom[st].prims[i].period_ms != 0) moving++;
    }
    snprintf(why, sizeof(why), "slot: state %u <- slot %u (%u bytes, %u moving)",
             st, slot, n, moving);
  }
  ble.send(MSG_LOG, 0, (const uint8_t*)why, (uint16_t)strlen(why));
}

// A face owns its background. Applying it here rather than once at boot is what
// makes the editor's 背景 colour picker reach the panel: setBackground() alone
// (the old code) only ran after the boot animation, so every face rendered on
// the compiled-in orange no matter what the uploaded JSON said.
static void applyFaceForState(uint8_t st) {
  if (st >= ST_COUNT) return;
  const Expression& f = haveCustom[st] ? custom[st] : *faces[st];
  rr.setBackground(f.bg);
  rr.setFace(&f);
}

static void setBacklight(uint8_t duty) {
  analogWrite(PIN_TFT_BL, duty);
}

// =====================================================================
// Frame handling
// =====================================================================
static void onFrame(const Frame& f) {
  // Any well-framed message proves the host is alive, even one we ignore.
  sm.noteHostActivity(millis());

  switch (f.type) {
    // A malformed payload used to be dropped in silence. That is the wrong
    // trade for a link with exactly one peer: an ACK_BADREQ costs a few bytes
    // and makes a protocol mismatch visible, whereas silence looks identical
    // to "the daemon never sent anything" — which has already cost several
    // debugging rounds on this project.
    case MSG_STATE:
      if (f.len < 1 || f.payload[0] >= ST_COUNT) {
        ble.ack(f.type, f.seq, ACK_BADREQ);
        break;
      }
      sm.onHostState(f.payload[0], millis());
      applyFaceForState(sm.visible());
      break;

    case MSG_CONFIG:
      if (f.len < 4) { ble.ack(f.type, f.seq, ACK_BADREQ); break; }
      store.setBrightness(f.payload[0]);
      store.setSpeed(f.payload[1]);
      store.setRotation(f.payload[2]);
      store.setIdleTimeoutS(f.payload[3]);
      setBacklight(store.brightness());
      // Rotation lands the same way brightness does: at once, unless the boot
      // reveal owns the panel. The reveal paints raw pixels with no dirty rect,
      // so a mid-reveal MADCTL change would remap every later segment; loop()
      // applies the saved value at hand-over instead.
      if (!boot.active()) {
        tft.setRotation(f.payload[2]);
        rr.invalidate();
      }
      break;

    case MSG_EXPR_BEGIN: {
      // slot, u16 len, u16 crc16
      if (f.len < 5) { ble.ack(f.type, f.seq, ACK_BADREQ); break; }
      exprSlot = f.payload[0];
      exprLen  = rd16(&f.payload[1]);
      exprCrc  = rd16(&f.payload[3]);
      exprType = f.type;
      exprSeq  = f.seq;
      exprGot  = 0;
      if (exprSlot >= SLOT_COUNT || exprLen == 0 || exprLen > EXPR_MAX_BYTES) {
        exprActive = false;
        ble.ack(f.type, f.seq, ACK_BADREQ);
        break;
      }
      exprActive = true;
      break;
    }

    case MSG_EXPR_CHUNK: {
      // slot, u16 off, data
      if (!exprActive || f.len < 3) { ble.ack(f.type, f.seq, ACK_BADREQ); break; }
      const uint8_t  slot = f.payload[0];
      const uint16_t off  = rd16(&f.payload[1]);
      const uint16_t n    = (uint16_t)(f.len - 3);
      if (slot != exprSlot || off != exprGot) {
        exprActive = false;
        ble.ack(f.type, f.seq, ACK_BADREQ);
        break;
      }
      if ((uint32_t)off + n > EXPR_MAX_BYTES) {
        exprActive = false;
        ble.ack(f.type, f.seq, ACK_NOSPACE);
        break;
      }
      memcpy(exprBuf + off, &f.payload[3], n);
      exprGot = (uint16_t)(off + n);
      break;
    }

    case MSG_EXPR_COMMIT:
      if (!exprActive) { ble.ack(f.type, f.seq, ACK_BADREQ); break; }
      exprActive = false;
      if (exprGot != exprLen) { ble.ack(f.type, f.seq, ACK_BADREQ); break; }
      if (crc16(exprBuf, exprGot) != exprCrc) {
        exprGot = 0;
        ble.ack(f.type, f.seq, ACK_CRC);
        break;
      }
      if (!store.writeSlot(exprSlot, exprBuf, exprGot)) { ble.ack(f.type, f.seq, ACK_NOSPACE); break; }
      ble.ack(f.type, f.seq, ACK_OK);
      break;

    // ── boot-file upload ───────────────────────────────────────
    // Deliberately parallel to the expression upload above, including its habit
    // of only acking failures mid-stream. Same reason: on a link that manages
    // ~10 KB/s, an ack per chunk would halve throughput for no information, and
    // the verdict that matters is COMMIT's.
    case MSG_BOOT_BEGIN:
      if (f.len < 5) { ble.ack(f.type, f.seq, ACK_BADREQ); break; }
      {
        const uint8_t  which = f.payload[0];
        const uint16_t len   = rd16(&f.payload[1]);
        const uint16_t want  = rd16(&f.payload[3]);
        bootWhich = which; bootLen = len; bootCrc = want;
        bootType = f.type; bootSeq = f.seq; bootGot = 0;
        if (which > BOOT_TARGET_META || len == 0 || len > BOOT_MAX_BYTES) {
          bootActive = false;
          ble.ack(f.type, f.seq, ACK_BADREQ);
          break;
        }
        bootActive = true;
      }
      break;

    case MSG_BOOT_CHUNK:
      if (!bootActive || f.len < 3) { ble.ack(f.type, f.seq, ACK_BADREQ); break; }
      {
        const uint8_t  which = f.payload[0];
        const uint16_t off   = rd16(&f.payload[1]);
        const uint16_t n     = (uint16_t)(f.len - 3);
        if (which != bootWhich || off != bootGot) {
          bootActive = false;
          ble.ack(f.type, f.seq, ACK_BADREQ);
          break;
        }
        if ((uint32_t)off + n > BOOT_MAX_BYTES) {
          bootActive = false;
          ble.ack(f.type, f.seq, ACK_NOSPACE);
          break;
        }
        memcpy(bootBuf + off, &f.payload[3], n);
        bootGot = (uint16_t)(off + n);
      }
      break;

    case MSG_BOOT_COMMIT:
      if (!bootActive) { ble.ack(f.type, f.seq, ACK_BADREQ); break; }
      bootActive = false;
      if (bootGot != bootLen) { ble.ack(f.type, f.seq, ACK_BADREQ); break; }
      // CRC over what actually arrived, so a chunk lost in flight is caught
      // here rather than half-drawing the logo on every boot.
      if (crc16(bootBuf, bootGot) != bootCrc) {
        bootGot = 0;
        ble.ack(f.type, f.seq, ACK_CRC);
        break;
      }
      if (!store.writeBootFile(bootWhich, bootBuf, bootGot)) {
        ble.ack(f.type, f.seq, ACK_NOSPACE);
        break;
      }
      ble.ack(f.type, f.seq, ACK_OK);
      {
        // Verify only when the last file lands.
        //
        // Reloading after every file reported "segs.bin missing" and "tris.bin
        // missing" for the two that had not been sent yet — which is true but
        // reads like a failure, and a log that cries wolf is worse than no log.
        // tris.bin is last in the upload order, so its commit is the moment
        // all three are guaranteed present.
        char why[56];
        snprintf(why, sizeof(why), "boot: %s written (%u bytes)",
                 bootWhich == BOOT_TARGET_META ? "meta.json"
                 : bootWhich == BOOT_TARGET_SEGS ? "segs.bin"
                                                 : "tris.bin",
                 bootGot);
        ble.send(MSG_LOG, 0, (const uint8_t*)why, (uint16_t)strlen(why));

        if (bootWhich == BOOT_TARGET_TRIS) {
          const char* verdict = boot.reload();
          if (verdict[0] != '\0') {
            ble.send(MSG_LOG, 0, (const uint8_t*)verdict, (uint16_t)strlen(verdict));
          }
        }
      }
      break;

    case MSG_BOOT_PLAY:
      // Replay without rebooting. The user just uploaded an animation and wants
      // to see it; "power cycle the panel to find out" is not feedback.
      boot.reload();
      boot.start(millis());
      ble.ack(f.type, f.seq, ACK_OK);
      break;

    case MSG_EXPR_SELECT:
      // 0xFF is the legal "unbind" value: store.stateSlot() already uses it as
      // the "use the compiled-in face" default, and loadCustomForState() treats
      // it that way. Rejecting it as an out-of-range slot made every unbind fail
      // with ACK_BADREQ, and because the host sends this without waiting for an
      // ACK the daemon reported success regardless — so the UI said "已解绑"
      // for a device that had never changed.
      if (f.len < 2 || f.payload[0] >= ST_COUNT ||
          (f.payload[1] != 0xFF && f.payload[1] >= SLOT_COUNT)) {
        ble.ack(f.type, f.seq, ACK_BADREQ);
        break;
      }
      store.setStateSlot(f.payload[0], f.payload[1]);
      loadCustomForState(f.payload[0]);
      applyFaceForState(sm.visible());
      ble.ack(f.type, f.seq, ACK_OK);
      break;

    case MSG_PING:
      // PONG hands the host's own timestamp back so the daemon can measure
      // round-trip time, plus enough info for the UI to know what it is
      // talking to without a separate handshake.
      //
      // The 4-byte minimum is not pedantry: an earlier revision sent an empty
      // payload, the firmware dropped it silently, and the daemon saw a link
      // that was up with a device that never answered anything.
      if (f.len < 4) { ble.ack(f.type, f.seq, ACK_BADREQ); break; }
      {
        uint8_t pl[8];
        memcpy(pl, &f.payload[0], 4);
        pl[4] = FW_VERSION_MAJOR;
        pl[5] = FW_VERSION_MINOR;
        pl[6] = SLOT_COUNT;
        pl[7] = store.usedSlots();
        ble.send(MSG_PONG, f.seq, pl, sizeof(pl));
      }
      break;

    default:
      ble.ack(f.type, f.seq, ACK_BADREQ);
      break;
  }
}

// =====================================================================
// Button
// =====================================================================
static void buttonTick(uint32_t now) {
  const bool down = (digitalRead(PIN_BTN) == LOW);
  if (down == btnLast) return;
  btnLast = down;

  if (down) {
    btnDownAt = now;
    btnLongFired = false;
    return;
  }

  if (btnLongFired) return;
  if (now - btnDownAt >= LONG_PRESS_MS) return;

  // Short press steps through the built-in faces. The preview is time-boxed:
  // without a deadline the last pressed face would stick forever, because
  // sm.tick() only repaints on a state change and an idle host never produces
  // one. PREVIEW_MS is what actually returns control to the host state.
  previewIdx = (uint8_t)((previewIdx + 1) % ST_COUNT);
  // No invalidate() here: setFace() diffs the new face against what is on
  // screen, and a full repaint would sweep the panel for a change the diff
  // can express locally.
  rr.setFace(faces[previewIdx]);
  previewUntil = now + PREVIEW_MS;
}

// Drops a button-initiated preview back to whatever the host reports. No-op
// once the deadline has passed or no preview is active. setBackground/setFace
// dedupe and diff on their own, so no invalidate() is needed here.
static void previewTick(uint32_t now) {
  if (previewUntil == 0 || now < previewUntil) return;
  previewUntil = 0;
  applyFaceForState(sm.visible());
}

static void buttonLongTick(uint32_t now) {
  if (!btnLast || btnLongFired) return;
  if (now - btnDownAt < LONG_PRESS_MS) return;
  btnLongFired = true;

  store.factoryReset();
  for (uint8_t i = 0; i < ST_COUNT; i++) loadCustomForState(i);
  previewUntil = 0;   // a reset is not a preview; stop previewTick from firing
  applyFaceForState(sm.visible());
}

// =====================================================================
// Boot animation — factory defaults
// =====================================================================
// A freshly flashed device has an empty LittleFS, so before this existed the
// animation was simply absent until someone uploaded it over BLE — ~3.4 KB at
// this link's speed, once per device, by hand. The compiled-in copy
// (boot_data.h, generated from tools\boot by tools\mochi_to_boot.py) is
// written once, when the files are missing.
//
// Once written, this never runs again: the files exist from then on, so an
// uploaded animation still replaces them exactly as before, and factoryReset()
// — which erases slots, not /boot — keeps the animation too. The one way to
// end up without it is to wipe the filesystem by hand, and then it comes back
// on the next boot, which is the point.
static void provisionBootDefaults() {
  if (!storeOk) return;
  if (LittleFS.exists(BOOT_META) && LittleFS.exists(BOOT_SEGS)) return;
  store.writeBootFilePgm(BOOT_TARGET_META, BOOT_DEFAULT_META, BOOT_DEFAULT_META_LEN);
  store.writeBootFilePgm(BOOT_TARGET_SEGS, BOOT_DEFAULT_SEGS, BOOT_DEFAULT_SEGS_LEN);
  store.writeBootFilePgm(BOOT_TARGET_TRIS, BOOT_DEFAULT_TRIS, BOOT_DEFAULT_TRIS_LEN);
}

// =====================================================================
// Arduino entry points
// =====================================================================
void setup() {
  Serial.begin(115200);
  pinMode(PIN_BTN, INPUT_PULLUP);
  pinMode(PIN_TFT_BL, OUTPUT);
  setBacklight(255);

  SPI.begin(PIN_TFT_SCK, -1, PIN_TFT_MOSI, PIN_TFT_CS);
  tft.init(DISP_W, DISP_H);
  tft.setSPISpeed(40000000);
  // Always via rgb565(), never a bare literal: the driver leaves RGB order
  // intact on this panel, and both the panel fill and the renderer's
  // background must be packed the same way or the bands will not match.
  tft.fillScreen(bgColor);
  rr.begin(tft, bgColor);

  storeOk = store.begin();

  // Before boot.begin(): loadMeta() runs in there and has to see the
  // provisioned files for the animation to be available() on a first boot.
  provisionBootDefaults();

  buildFaces();

  // Start from OFFLINE (hostOnline_ is false after begin) so a device left
  // powered on does not pretend to be linked to a daemon that is not running.
  sm.begin(millis());
  setBacklight(store.brightness());
  // init() ends with setRotation(0), so the orientation saved in NVS has to be
  // re-applied here — after both tft.init() and store.begin(), in that order.
  // From this point on every face, band and text runs through the driver's
  // MADCTL transform, so nothing in the renderer needs to know about it.
  tft.setRotation(store.rotation());

  // The start-up animation owns the panel from here on. It must be started
  // BEFORE anything paints a face, because the face would otherwise be drawn
  // underneath it and stay visible: the reveal only adds segments, so an
  // OFFLINE face drawn first shows through every gap in the logo for the whole
  // three seconds. That is the "closed eyes icon overlapping the animation"
  // bug — faceOffline is exactly two horizontal bars, and they read as closed
  // eyes.
  //
  // Where the animation is absent (no /boot files), the old behaviour is kept:
  // the face is painted now, so the panel is never blank.
  boot.begin(rr, bgColor);
  if (boot.available()) {
    boot.start(millis());
  } else {
    applyFaceForState(sm.visible());
  }

  rr.setStatus(ble.connected(), sm.hostOnline(), "BOOTING");
  rr.tick(millis());

  // The animation and the BLE stack both start from loop(), so the ordering
  // rule — animation first, link after — lives in exactly one place:
  //
  //   with /boot files    the animation runs, then BLE starts when it ends
  //   without them        BLE starts on the very first loop()
  //
  // Starting both at once would let a hook state arriving two frames into the
  // reveal overwrite the logo, so the user would see a flash of the wrong face
  // on every boot. ~3 seconds of not-yet-advertising is the cheaper trade, and
  // it also keeps the reveal off the one core this chip has to share with BLE.
  //
  // Everything that used to live here — loadCustomForState(), the storage log
  // line, the "firmware ready" printf — moved into loop(), after BLE is
  // actually up. Running them in setup() sent frames over a stack that was not
  // started yet, which is why the original comment said they were "silently
  // dropped".
}

void loop() {
  const uint32_t now = millis();

  // ── BLE comes up after the animation, or at once if there is none ──────────
  //
  // Checked before the animation branch, not inside it: a device with no /boot
  // files has boot.active() == false from the first loop() call, so anything
  // gated on that would never run — and such a device would sit dark forever,
  // which is the single most expensive failure in this file.
  if (!bleStarted_) {
    if (boot.active()) {
      // Still playing. Let it run; check again next iteration.
    } else {
      ble.begin(DEVICE_NAME, onFrame);
      bleStarted_ = true;

      // Custom faces load after the link is up because loadCustomForState()
      // reports each state's outcome over MSG_LOG, and those frames are dropped
      // while the BLE stack is not running.
      for (uint8_t i = 0; i < ST_COUNT; i++) loadCustomForState(i);

      // One log line at boot: storage is the thing most likely to be wrong after
      // a partition change, and it is invisible from the outside.
      if (storeOk) ble.send(MSG_LOG, 0, (const uint8_t*)"store:mounted", 14);
      else         ble.send(MSG_LOG, 0, (const uint8_t*)"store:fail,slots off", 22);

      Serial.printf("firmware %d.%d ready, %d slots in use, store=%d\n",
                    FW_VERSION_MAJOR, FW_VERSION_MINOR, store.usedSlots(), storeOk);
    }
  }

  // ── the start-up animation owns the panel until it ends ────────
  if (boot.active()) {
    boot.tick(now);
    // The animation draws raw pixels, so nothing else is painting. Keep the
    // status bands current anyway: a panel that shows no BLE state during boot
    // looks broken, and this is the whole reason the bands exist.
    rr.setStatus(ble.connected(), sm.hostOnline(), sm.stateName());
    rr.tick(now);

    if (!boot.active()) {
      // Finished. Clear to the real background and hand over to the caller.
      // Orientation saved while the reveal was running is applied now: the
      // reveal owns raw drawing, so it had to wait for this moment.
      //
      // The invalidate() is load-bearing here and stays: the panel is covered
      // in raw animation pixels that no face history describes, so the face
      // diff has nothing truthful to diff against — a full repaint is the only
      // safe handover.
      rr.setBackground(bgColor);
      tft.setRotation(store.rotation());
      applyFaceForState(sm.visible());
      rr.invalidate();
      rr.setStatus(ble.connected(), sm.hostOnline(), sm.stateName());
      rr.tick(now);
    }
    return;
  }

  ble.poll();

  // Rate-limit the renderer to FRAME_TICK_MS. Without this, tick() ran on every
  // loop iteration — thousands of times a second — which meant the dirty-rect
  // bookkeeping, the visibility comparison and the memcpy of three small arrays
  // all ran far more often than anything could be seen, competing with the BLE
  // stack for the single core. FRAME_TICK_MS existed for exactly this and was
  // never referenced.
  static uint32_t lastDraw = 0;

  bool due = (uint32_t)(now - lastDraw) >= FRAME_TICK_MS;

  // A face change must not wait for the frame budget: the user just got a new
  // state and seeing the old face for another 33ms reads as lag.
  if (sm.tick(now)) {
    applyFaceForState(sm.visible());
    due = true;
  }

  rr.setStatus(ble.connected(), sm.hostOnline(), sm.stateName());
  if (due) {
    lastDraw = now;
    rr.tick(now);
  }

  // Self-check, once, after the first second. A renderer that has painted
  // nothing by then is wedged, and reporting that beats a blank screen.
  if (!selfCheckDone && now >= 1000) {
    selfCheckDone = true;
    if (rr.paintedFrames() == 0) selfCheckFail = 1;
    else if (!storeOk)           selfCheckFail = 2;
    if (selfCheckFail) {
      const char* why = (selfCheckFail == 1) ? "render:stalled" : "render:no-store";
      ble.send(MSG_LOG, 0, (const uint8_t*)why, (uint16_t)strlen(why));
    }
  }

  buttonTick(now);
  buttonLongTick(now);
  previewTick(now);

  // STATUS heartbeat. The daemon reads this to detect a live link even when
  // nothing changes, and to notice the host timeout window opening.
  static uint32_t lastBeat = 0;
  static uint8_t  beatSeq  = 0;
  if (now - lastBeat >= HEARTBEAT_MS) {
    lastBeat = now;
    const uint8_t pl[3] = { (uint8_t)sm.visible(), ble.connected() ? 1 : 0, lastErr };
    ble.send(MSG_STATUS, ++beatSeq, pl, sizeof(pl));
  }
}
