// expression.h — the on-device expression model and its JSON loader.
//
// Header-only (see store.h). Both the BLE path (device receives) and the
// built-in faces (compiled in) populate the same structs, so there is exactly
// one renderer and one code path for "show a face".
//
// Why this shape:
//   - Primitives, not bitmaps, for v1. A 240x240 RGB565 frame is 115KB and
//     will not fit in EXPR_MAX_BYTES; a parametric face is ~1-2KB and travels
//     in one or two BLE chunks.
//   - Colours are pre-resolved to RGB565 at parse time. The panel is a fixed
//     16-bit device, so there is no reason to keep RGB triples around and
//     convert per pixel per frame.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include "config.h"

enum PrimType : uint8_t {
  PRIM_RECT = 0,
  PRIM_CIRCLE,
  PRIM_LINE,
  PRIM_POLY,
  PRIM_TEXT,
  PRIM_TYPE_COUNT
};

enum Fx : uint8_t {
  FX_NONE = 0,
  FX_BLINK,   // visible only during the first on_ms of each period
  FX_PULSE,   // scale by amount/100 (100 = no change)
  FX_SHAKE,   // jitter x by amount pixels
  FX_SPIN,    // rotate points by amount degrees each period (lines/polys)
  FX_FADE,    // alpha toward background by amount/100
  FX_COUNT
};

struct Prim {
  PrimType type;
  uint16_t color;
  int16_t  x, y;    // rect: x,y   circle: cx,cy   line: x1,y1   text: x,y
  int16_t  x2, y2;  // rect: w,h   line:   x2,y2
  uint8_t  size;    // text scale
  char     text[TEXT_MAX_LEN];
  uint8_t  npts;    // polygon point count
  int16_t  pts[MAX_POINTS * 2];

  Fx       fx;
  uint16_t period_ms;
  uint16_t on_ms;
  int16_t  amount;  // percent for PULSE/FADE, px for SHAKE, degrees for SPIN

  // Set once at resolve time; the renderer uses it to skip work.
  bool     animating;
};

struct Expression {
  // The background this face is drawn on. Parsed from the JSON's "bg" field,
  // with the caller's current background as the fallback for faces that do not
  // specify one — the seven built-in faces rely on that fallback.
  //
  // It exists because the editor has always shipped a background colour picker
  // that changed only the preview: the firmware ignored the field entirely and
  // cleared to its compiled-in orange, so a face uploaded with a blue background
  // still rendered on orange. The panel and the editor disagreed, with nothing
  // on either side saying so.
  uint16_t bg;
  uint8_t nprims;
  Prim     prims[MAX_PRIMS];
};

// ── Colour ────────────────────────────────────────────────────
// Accepts "#RGB", "#RRGGBB", and "#RRGGBBAA" (alpha ignored: the panel has no
// blending, and FX_FADE covers the one case where we want it).
inline uint16_t exprParseColor(const char* hex, uint16_t fallback) {
  if (!hex || hex[0] != '#' || hex[1] == '\0') return fallback;

  uint32_t v = 0;
  const char* p = hex + 1;
  int digits = 0;
  while (*p && digits < 8) {
    const char c = (char)(*p | 0x20);
    if (c >= '0' && c <= '9')      v = (v << 4) | (uint32_t)(c - '0');
    else if (c >= 'a' && c <= 'f') v = (v << 4) | (uint32_t)(c - 'a' + 10);
    else return fallback;
    p++; digits++;
  }
  if (digits == 3) {  // #RGB expand to #RRGGBB
    const uint8_t r = (uint8_t)((v >> 8) & 0x0F) * 17;
    const uint8_t g = (uint8_t)((v >> 4) & 0x0F) * 17;
    const uint8_t b = (uint8_t)((v     ) & 0x0F) * 17;
    return ((uint16_t)r << 11) | ((uint16_t)g << 5) | (uint16_t)b;
  }
  if (digits < 6) return fallback;

  // Hex components are 8-bit, but RGB565 stores 5/6/5. Shifting an 8-bit value
  // straight into the top 5 bits overflows: 90 << 11 is 184320, which truncates
  // to garbage in a uint16_t. #000 and #FFFFFF survive by accident (both land
  // on the endpoints); every intermediate colour is wrong.
  const uint8_t r = (uint8_t)(v >> 16);
  const uint8_t g = (uint8_t)(v >> 8);
  const uint8_t b = (uint8_t)(v);
  return (uint16_t)(((uint16_t)(r >> 3) << 11) |
                    ((uint16_t)(g >> 2) << 5) |
                    (uint16_t)(b >> 3));
}

// ── JSON loader ───────────────────────────────────────────────
// Expected shape (see DESIGN.md 5.4). Anything unrecognised is skipped rather
// than failing the whole face: a partial face beats a dark screen.
//
// `why`, when supplied, receives a static string naming the failure. It exists
// because a bare `false` collapsed three unrelated causes — heap pressure, a
// malformed document, and a missing layers array — into one signal, and the
// caller then reported "json parse failed" for all three, which pointed at the
// JSON when the real cause was memory. A failure with no reason is a failure
// that gets misdiagnosed.
//
// `fallback_bg` is the background to use when the document has no "bg" field.
// Passing the caller's current background means an older face (written before
// the field existed) keeps rendering on whatever the panel already showed,
// instead of snapping to black.
inline bool parseExpression(const uint8_t* json, uint16_t len, Expression& out,
                            uint16_t fallback_bg, const char** why = nullptr) {
  out.nprims = 0;
  out.bg = fallback_bg;
  auto fail = [&why](const char* reason) {
    if (why) *why = reason;
    return false;
  };

  if (len == 0 || len > EXPR_MAX_BYTES) return fail("blob length out of range");

  // Heap guard. A 4KB document needs roughly 2-3x that in ArduinoJson's pool
  // on 7.x. Check before parsing, because a pool failure inside the parser
  // surfaces as the same DeserializationError as malformed JSON — and the
  // caller would then blame the JSON for a memory problem.
  if (ESP.getFreeHeap() < (size_t)len * 3 + 1024) return fail("device out of memory");

  // Plain dynamic JsonDocument, portable across every 7.x. The capacity-taking
  // constructor was left behind when 7.2 replaced the pool allocator with a
  // slot-list one, so it is deliberately not used here.
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, (const char*)json, len);
  if (err) return fail("json did not parse");
  if (!doc.is<JsonObject>()) return fail("top level is not an object");

  JsonObject root = doc.as<JsonObject>();
  JsonArray layers = root["layers"];
  if (layers.isNull()) return fail("no layers array");

  // "bg" is optional, and an absent or malformed value keeps the fallback.
  // exprParseColor already returns its fallback for anything that is not a
  // #rrggbb triple, so a hand-edited "bg": "orange" degrades instead of failing
  // the whole face — which would leave the previous face on screen.
  out.bg = exprParseColor(root["bg"] | "", fallback_bg);

  for (JsonObject l : layers) {
    if (out.nprims >= MAX_PRIMS) break;

    Prim p;
    memset(&p, 0, sizeof(p));

    const char* t = l["type"] | "";
    uint16_t col = exprParseColor(l["color"] | "#000000", 0x0000);

    if (strcmp(t, "rect") == 0) {
      p.type = PRIM_RECT;
      p.x  = (int16_t)(l["x"] | 0);
      p.y  = (int16_t)(l["y"] | 0);
      p.x2 = (int16_t)(l["w"] | 0);
      p.y2 = (int16_t)(l["h"] | 0);
    } else if (strcmp(t, "circle") == 0) {
      p.type = PRIM_CIRCLE;
      p.x  = (int16_t)(l["cx"] | 0);
      p.y  = (int16_t)(l["cy"] | 0);
      p.x2 = (int16_t)(l["r"]  | 0);
    } else if (strcmp(t, "line") == 0) {
      p.type = PRIM_LINE;
      p.x  = (int16_t)(l["x1"] | 0);
      p.y  = (int16_t)(l["y1"] | 0);
      p.x2 = (int16_t)(l["x2"] | 0);
      p.y2 = (int16_t)(l["y2"] | 0);
    } else if (strcmp(t, "poly") == 0) {
      p.type = PRIM_POLY;
      // v7 has no const variant of the array proxy; the element type is
      // JsonObject and readonly-ness is not a compile-time property here.
      JsonArray a = l["points"];
      if (a.isNull()) continue;
      uint8_t i = 0;
      for (JsonVariant pt : a) {
        if (i >= MAX_POINTS) break;
        p.pts[i * 2    ] = (int16_t)(pt[0] | 0);
        p.pts[i * 2 + 1] = (int16_t)(pt[1] | 0);
        i++;
      }
      if (i < 3) continue;           // a 2-point poly is a line, not an area
      p.npts = i;
    } else if (strcmp(t, "text") == 0) {
      p.type = PRIM_TEXT;
      p.x    = (int16_t)(l["x"] | 0);
      p.y    = (int16_t)(l["y"] | 0);
      p.size = (uint8_t)(l["size"] | 2);
      const char* txt = l["text"] | "";
      strncpy(p.text, txt, TEXT_MAX_LEN - 1);
      p.text[TEXT_MAX_LEN - 1] = '\0';
      if (p.text[0] == '\0') continue;
    } else {
      continue;
    }

    p.color = col;
    // The effect arrives as a name from the editor and from DESIGN.md
    // ("effect": "blink"), and as a raw enum index from anything writing it
    // numerically. Both are accepted.
    //
    // Reading a name as an integer was the bug this replaces: `l["effect"] | 0`
    // converts the string "blink" to 0, which is FX_NONE, so every uploaded face
    // silently lost its animation while the compiled-in faces kept theirs — they
    // set the field directly in C++. A face that animates nothing is
    // indistinguishable from one that was never given an effect.
    //
    // JsonString rather than is<const char*>(): a document parsed from a const
    // buffer stores its strings as const, and is<const char*>() returns false
    // for those — which would silently send every name down the numeric path and
    // back to the original bug.
    Fx fx = FX_NONE;
    const JsonString es = l["effect"].as<JsonString>();
    if (!es.isNull()) {
      const char* s = es.c_str();
      if (s != nullptr) {
        if (strcmp(s, "blink") == 0)      fx = FX_BLINK;
        else if (strcmp(s, "pulse") == 0) fx = FX_PULSE;
        else if (strcmp(s, "shake") == 0) fx = FX_SHAKE;
        else if (strcmp(s, "spin") == 0)  fx = FX_SPIN;
        else if (strcmp(s, "fade") == 0)  fx = FX_FADE;
        // "none" and anything unrecognised stay FX_NONE: an unknown effect
        // degrades to a static layer rather than failing the whole face.
      }
    } else {
      const int en = l["effect"] | (int)FX_NONE;
      if (en > 0 && en < FX_COUNT) fx = (Fx)en;
    }
    p.fx = fx;
    if (p.fx >= FX_COUNT) p.fx = FX_NONE;
    p.period_ms = (uint16_t)(l["period_ms"] | 0);
    p.on_ms     = (uint16_t)(l["on_ms"]     | 0);
    p.amount    = (int16_t)(l["amount"]     | 100);
    if (p.period_ms == 0) { p.fx = FX_NONE; p.amount = 100; }
    p.animating = (p.fx != FX_NONE);

    out.prims[out.nprims++] = p;
  }

  return out.nprims > 0;
}
