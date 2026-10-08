// protocol.h — Claude HUD wire codec v1.
//
// Header-only on purpose: Arduino concatenates every file in the sketch folder,
// and keeping the codec free of .cpp state makes it trivially unit-testable on
// the host (the daemon must pass the exact same test vectors).
//
// Frame layout (little-endian fields):
//
//   +------+------+-----+------+-----+--------+--------+---------+-----+
//   | 0xA5 | 0x5A | VER | TYPE | SEQ | LEN_LO | LEN_HI | PAYLOAD | CRC8|
//   +------+------+-----+------+-----+--------+--------+---------+-----+
//          \___________________ CRC8 covers this whole span ___________/
//
// SOF bytes are excluded from the CRC so that a resynchronising parser can
// find the frame start without knowing the length.
#pragma once
#include <Arduino.h>
#include <string.h>
#include "config.h"

// ── CRC ───────────────────────────────────────────────────────
// CRC-8/SMBUS: poly 0x07, init 0x00, no reflection, no final xor.
inline uint8_t crc8(const uint8_t* d, size_t n) {
  uint8_t c = 0x00;
  while (n--) {
    c ^= *d++;
    for (uint8_t i = 0; i < 8; i++) {
      c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x07) : (uint8_t)(c << 1);
    }
  }
  return c;
}

// CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final xor.
// Used to verify a whole expression blob at EXPR_COMMIT time.
inline uint16_t crc16(const uint8_t* d, size_t n) {
  uint16_t c = 0xFFFF;
  while (n--) {
    c ^= (uint16_t)(*d++) << 8;
    for (uint8_t i = 0; i < 8; i++) {
      c = (c & 0x8000) ? (uint16_t)((c << 1) ^ 0x1021) : (uint16_t)(c << 1);
    }
  }
  return c;
}

// ── Byte order helpers ────────────────────────────────────────
inline uint16_t rd16(const uint8_t* p) {
  return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}
inline uint32_t rd32(const uint8_t* p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
         ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
inline void wr16(uint8_t* p, uint16_t v) {
  p[0] = (uint8_t)(v & 0xFF);
  p[1] = (uint8_t)(v >> 8);
}
inline void wr32(uint8_t* p, uint32_t v) {
  p[0] = (uint8_t)(v & 0xFF);
  p[1] = (uint8_t)((v >> 8) & 0xFF);
  p[2] = (uint8_t)((v >> 16) & 0xFF);
  p[3] = (uint8_t)((v >> 24) & 0xFF);
}

// ── Decoded frame ─────────────────────────────────────────────
struct Frame {
  uint8_t  type;
  uint8_t  seq;
  uint16_t len;
  uint8_t  payload[FRAME_MAX_PAYLOAD];
};

// frameDecode result. Positive values are a consumed-byte count, which is what
// a stream parser needs; the two negative/zero cases must stay distinguishable
// so the caller knows whether to wait or to resynchronise.
enum : int16_t {
  FP_BAD       = -1,  // not a valid frame here — drop one byte and retry
  FP_NEED_MORE =  0,  // prefix of a plausible frame — wait for more bytes
};

// Attempts to decode one frame from buf[0..n).
// Returns bytes consumed (>0), FP_NEED_MORE, or FP_BAD.
inline int16_t frameDecode(const uint8_t* b, uint16_t n, Frame& out) {
  if (n < FRAME_HDR + 1) return FP_NEED_MORE;
  if (b[0] != FRAME_SOF0 || b[1] != FRAME_SOF1) return FP_BAD;
  if (b[2] != FRAME_VER) return FP_BAD;

  const uint16_t len = rd16(&b[5]);
  if (len > FRAME_MAX_PAYLOAD) return FP_BAD;

  const uint16_t total = (uint16_t)(FRAME_HDR + len + 1);
  if (n < total) return FP_NEED_MORE;

  if (crc8(&b[2], (size_t)(FRAME_HDR - 2 + len)) != b[FRAME_HDR + len]) return FP_BAD;

  out.type = b[3];
  out.seq  = b[4];
  out.len  = len;
  if (len) memcpy(out.payload, &b[FRAME_HDR], len);
  return (int16_t)total;
}

// Encodes a frame into out[0..cap). Returns bytes written, or 0 if it will not fit.
inline uint16_t frameEncode(uint8_t type, uint8_t seq,
                            const uint8_t* payload, uint16_t len,
                            uint8_t* out, uint16_t cap) {
  if (len > FRAME_MAX_PAYLOAD) return 0;
  const uint16_t total = (uint16_t)(FRAME_HDR + len + 1);
  if (cap < total) return 0;

  out[0] = FRAME_SOF0;
  out[1] = FRAME_SOF1;
  out[2] = FRAME_VER;
  out[3] = type;
  out[4] = seq;
  wr16(&out[5], len);
  if (len) memcpy(&out[FRAME_HDR], payload, len);
  out[FRAME_HDR + len] = crc8(&out[2], (size_t)(FRAME_HDR - 2 + len));
  return total;
}

// A frameMaxChunk() helper used to live here. It was never called, and it
// carried the same bug the daemon's version had: a floor of 16 bytes on the
// chunk budget, which at a low MTU produces a frame larger than the peer can
// accept. Chunk sizing is the daemon's job — it is the sender — and its version
// in hud_daemon/protocol.py clamps to min(att_room, MAX_PAYLOAD - 3) with no
// artificial floor. Deleting this rather than fixing it, because a function
// nobody calls is a trap: it reads like a guarantee that is not being enforced.
