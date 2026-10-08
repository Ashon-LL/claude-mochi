// config.h — Claude HUD firmware: pins, BLE identifiers, protocol constants.
// Hardware: ESP32-C3 SuperMini (4MB flash) + ST7789 240x240.
// Wiring per D:\Claude DIY\接线.xlsx — do not change these pins without updating that sheet.
#pragma once
#include <Arduino.h>

// ── Identity ──────────────────────────────────────────────────
// The version is the only way to tell whether a flashed device carries a given
// fix. It stayed at 1.0 through several behavioural changes, which made "I
// changed the firmware, try again" unverifiable — the app reported the same
// number either way, so a stale device was indistinguishable from an unfixed
// one. The app reads this from the PONG payload and shows it in the status bar.
//
//   1.0  original rewrite: animated faces, status bands, expression upload
//   1.1  effect names parse as names, visibility flips repaint locally instead
//        of clearing the screen, and FX_SHAKE is a dwell-and-sweep gaze
//   1.2  rotation is applied, not only stored: setRotation() on the saved NVS
//        value in setup() and on MSG_CONFIG, so the panel can be turned
//        90 degrees without reflashing a third time
//   1.3  an unchanged animating primitive no longer repaints its box: a blink
//        lid at rest wiped its whole region every frame, which read as a
//        shimmer over the eye
#define FW_VERSION_MAJOR 1
#define FW_VERSION_MINOR 3
#define DEVICE_NAME      "Claude-HUD"

// ── Display pins (ST7789) ─────────────────────────────────────
// BL is mandatory: with it floating the panel stays dark and looks "dead".
#define PIN_TFT_BL    3
#define PIN_TFT_CS    4
#define PIN_TFT_DC    1
#define PIN_TFT_RST   2
#define PIN_TFT_SCK   8
#define PIN_TFT_MOSI 10
#define PIN_BTN       9   // BOOT key, strapping pin: holding it at power-on enters download mode

#define DISP_W 240
#define DISP_H 240

// ── BLE UUIDs ─────────────────────────────────────────────────
// Service and RX are deliberately unchanged from the first firmware build.
// Windows caches a device's GATT table; changing them forces the user to
// unpair and re-pair. Only TX is new.
#define BLE_SERVICE_UUID "12345678-1234-1234-1234-123456789abc"
#define BLE_RX_UUID      "12345678-1234-1234-1234-123456789abd"  // host -> device, WRITE
#define BLE_TX_UUID      "12345678-1234-1234-1234-123456789abe"  // device -> host, NOTIFY
#define BLE_MTU_WANTED   512

// ── Frame protocol v1 ─────────────────────────────────────────
// +------+------+-----+------+-----+--------+---------+-----+
// | 0xA5 | 0x5A | VER | TYPE | SEQ | LEN_LO | LEN_HI  | ... |
// +------+------+-----+------+-----+--------+---------+-----+
// | PAYLOAD (LEN bytes)                       | CRC8       |
// +-------------------------------------------+------------+
// CRC8 poly 0x07, computed over VER..PAYLOAD (i.e. everything after the 2 SOF bytes).
#define FRAME_SOF0 0xA5
#define FRAME_SOF1 0x5A
#define FRAME_VER  0x01
#define FRAME_HDR  7         // SOF0 SOF1 VER TYPE SEQ LEN_LO LEN_HI
#define FRAME_MAX_PAYLOAD 256

enum MsgType : uint8_t {
  MSG_STATE       = 0x01,  // h->d  u8 state, u8 flags
  MSG_EXPR_BEGIN  = 0x02,  // h->d  u8 slot, u16 len, u16 crc16, u16 raw_len
  MSG_EXPR_CHUNK  = 0x03,  // h->d  u8 slot, u16 off, u8 data[]
  MSG_EXPR_COMMIT = 0x04,  // h->d  u8 slot
  MSG_EXPR_SELECT = 0x05,  // h->d  u8 state, u8 slot
  MSG_CONFIG      = 0x06,  // h->d  u8 brightness, u8 speed, u8 rotation, u8 idle_timeout_s
  MSG_PING        = 0x07,  // h->d  u32 ts_ms
  MSG_TIME_SYNC   = 0x08,  // h->d  u32 unix_ts
  MSG_PONG        = 0x10,  // d->h  u32 ts, u8 fw_major, u8 fw_minor, u8 slot_count, u8 used_slots
  MSG_ACK         = 0x11,  // d->h  u8 acked_type, u8 acked_seq, u8 code
  MSG_STATUS      = 0x12,  // d->h  u8 cur_state, u8 ble_connected, u8 last_err
  MSG_LOG         = 0x13,  // d->h  utf8 text (debug)

  // ── boot animation upload ─────────────────────────────────────
  // Same three-phase shape as the expression upload (BEGIN carries the total
  // length and a CRC16, CHUNKs stream, COMMIT is acknowledged with the verdict)
  // because a 3 KB binary at ~10 KB/s over BLE needs exactly that: a length
  // check up front, ordered chunks, and one integrity check at the end.
  //
  // which: 0 = /boot/segs.bin  1 = /boot/tris.bin  2 = /boot/meta.json
  MSG_BOOT_BEGIN  = 0x20,  // h->d  u8 which, u16 len, u16 crc16
  MSG_BOOT_CHUNK  = 0x21,  // h->d  u8 which, u16 off, u8 data[]
  MSG_BOOT_COMMIT = 0x22,  // h->d  u8 which
  MSG_BOOT_PLAY   = 0x23,  // h->d  (empty) replay it now, without a reboot
};

#define ACK_OK      0
#define ACK_CRC     1
#define ACK_NOSPACE 2
#define ACK_BADREQ  3

// ── States ────────────────────────────────────────────────────
enum HudState : uint8_t {
  ST_IDLE = 0,
  ST_THINKING,
  ST_TOOL_START,
  ST_TOOL_END,
  ST_WAITING,
  ST_ERROR,
  ST_OFFLINE,
  ST_COUNT
};

// ── Timing ────────────────────────────────────────────────────
#define TOOL_END_HOLD_MS 1000UL   // TOOL_END is transient: auto-revert to THINKING
#define HOST_TIMEOUT_MS 30000UL   // no frame from host -> OFFLINE
#define HEARTBEAT_MS     5000UL
#define FRAME_TICK_MS      33UL   // ~30fps animation cap; dirty rects keep this cheap
#define LONG_PRESS_MS    3000UL

// ── Layout reserved for the always-on status badge ────────────
#define BADGE_H   22   // top-right: BLE link indicator
#define STATUS_H  16   // bottom strip: host link + current state name

// ── Storage ───────────────────────────────────────────────────
#define SLOT_COUNT      12
#define EXPR_MAX_BYTES 4096
#define STORE_PATH     "/expr"

// Boot-animation files, also in LittleFS but with their own ceiling. Both a
// 162-segment logo and a longer one fit: segs.bin is 8 bytes per segment and
// tris.bin 12 per triangle, so 8192 covers roughly 1000 of either.
#define BOOT_MAX_BYTES 8192

// Boot upload targets, used by MSG_BOOT_* and store.writeBootFile().
#define BOOT_TARGET_SEGS 0
#define BOOT_TARGET_TRIS 1
#define BOOT_TARGET_META 2

// Paths, here rather than in boot_anim.h because store.h writes these files and
// is included before boot_anim.h — a macro defined by a later header is not
// visible to an earlier one.
#define BOOT_PATH     "/boot"
#define BOOT_META     BOOT_PATH "/meta.json"
#define BOOT_SEGS     BOOT_PATH "/segs.bin"
#define BOOT_TRIS     BOOT_PATH "/tris.bin"

// ── Renderer limits ───────────────────────────────────────────
#define MAX_PRIMS  16
#define MAX_POINTS  16
#define TEXT_MAX_LEN 24
