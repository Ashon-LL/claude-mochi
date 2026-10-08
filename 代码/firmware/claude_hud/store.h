// store.h — on-device persistence: expression slots + small settings.
//
// Header-only, like protocol.h and state_machine.h, so the sketch folder needs
// no .cpp bookkeeping and the logic stays host-testable.
//
// Two stores on purpose:
//   LittleFS /expr/slotNN.bin   expression JSON (up to 4KB each), because NVS
//                               is a blob store with a 5044-byte entry limit and
//                               poor behaviour under repeated rewrites
//   Preferences "cchud"          the handful of settings that must survive a
//                               reboot and must be available before FS mounts
//
// Slot writes are atomic: build slotNN.bin.tmp, verify its length, then rename.
// A slot is only ever listed as present once the rename succeeded, so a power
// cut mid-transfer leaves the previous expression intact instead of a truncated
// one that JSONParser would choke on.
#pragma once
#include <Arduino.h>
#include <LittleFS.h>
#include <Preferences.h>
#include "config.h"

class Store {
public:
  bool begin() {
    // (true) formats on mount failure, which is what we want on the very first
    // boot after a partition change; every later mount is a real mount.
    if (!LittleFS.begin(true)) return false;
    LittleFS.mkdir(STORE_PATH);
    prefs_.begin(NS, /*readOnly=*/false);
    return true;
  }

  // ── Expression slots ────────────────────────────────────────

  // Reads one slot into buf. Returns false if the slot is empty, absent, or
  // longer than cap — never returns truncated JSON.
  bool readSlot(uint8_t slot, uint8_t* buf, uint16_t cap, uint16_t& outLen) const {
    outLen = 0;
    if (slot >= SLOT_COUNT) return false;

    char path[24];
    slotPath(slot, path);
    File f = LittleFS.open(path, "r");
    if (!f) return false;

    const size_t n = f.size();
    if (n == 0 || n > cap || n > EXPR_MAX_BYTES) { f.close(); return false; }

    const size_t got = f.read(buf, n);
    f.close();
    if (got != n) return false;

    outLen = (uint16_t)n;
    return true;
  }

  bool slotPresent(uint8_t slot) const {
    if (slot >= SLOT_COUNT) return false;
    char path[24];
    slotPath(slot, path);
    return LittleFS.exists(path) && LittleFS.open(path, "r").size() > 0;
  }

  // Atomic: write the .tmp, then rename over the slot.
  bool writeSlot(uint8_t slot, const uint8_t* data, uint16_t len) {
    if (slot >= SLOT_COUNT || len == 0 || len > EXPR_MAX_BYTES) return false;

    char tmp[24];
    tmpPath(slot, tmp);
    File f = LittleFS.open(tmp, "w");
    if (!f) return false;

    const size_t put = f.write(data, len);
    f.close();
    if (put != len) { LittleFS.remove(tmp); return false; }

    char path[24];
    slotPath(slot, path);
    if (!LittleFS.rename(tmp, path)) { LittleFS.remove(tmp); return false; }
    return true;
  }

  bool eraseSlot(uint8_t slot) {
    if (slot >= SLOT_COUNT) return false;
    char path[24];
    slotPath(slot, path);
    return LittleFS.remove(path);
  }

  // 0xFF in a state->slot binding means "use the compiled-in face for this
  // state" — the firmware always works with zero slots written.
  // These read NVS, which the Preferences API exposes only as non-const.
  uint8_t stateSlot(uint8_t state) {
    if (state >= ST_COUNT) return 0xFF;
    char key[8];
    snprintf(key, sizeof(key), "st%u", (unsigned)state);
    return prefs_.getUChar(key, 0xFF);
  }

  void setStateSlot(uint8_t state, uint8_t slot) {
    if (state >= ST_COUNT) return;
    char key[8];
    snprintf(key, sizeof(key), "st%u", (unsigned)state);
    prefs_.putUChar(key, slot);
  }

  // ── Settings ────────────────────────────────────────────────
  // Not const: Preferences exposes no const getters, and faking constness with
  // mutable members buys nothing here.
  uint8_t brightness()   { return prefs_.getUChar("br", 160); }
  uint8_t speed()        { return prefs_.getUChar("spd", 2); }
  uint8_t rotation()     { return prefs_.getUChar("rot", 1); }
  uint8_t idleTimeoutS() { return prefs_.getUChar("it", 30); }

  void setBrightness(uint8_t v)   { prefs_.putUChar("br", v); }
  void setSpeed(uint8_t v)        { prefs_.putUChar("spd", v); }
  void setRotation(uint8_t v)     { prefs_.putUChar("rot", v); }
  void setIdleTimeoutS(uint8_t v) { prefs_.putUChar("it", v); }

  // Wipes every slot and every custom binding. Used by long-press on BOOT.
  void factoryReset() {
    for (uint8_t s = 0; s < SLOT_COUNT; s++) {
      eraseSlot(s);
      setStateSlot(s, 0xFF);
    }
  }

  uint8_t usedSlots() {
    uint8_t n = 0;
    for (uint8_t s = 0; s < SLOT_COUNT; s++) if (slotPresent(s)) n++;
    return n;
  }

  // ── boot animation files ────────────────────────────────────
  // Boot files are not slots: they are raw binaries the animation streams, so
  // they have their own names and a much larger ceiling. The write pattern is
  // identical to writeSlot's — temp file, then rename — because the failure it
  // guards is the same: a power cut mid-upload must leave the previous file
  // intact, not a truncated one that half-draws a logo on every boot.
  //
  // which: BOOT_TARGET_SEGS, BOOT_TARGET_TRIS or BOOT_TARGET_META.
  bool writeBootFile(uint8_t which, const uint8_t* data, uint16_t len) {
    if (which > 2 || len == 0 || len > BOOT_MAX_BYTES) return false;
    if (!LittleFS.exists(BOOT_PATH)) LittleFS.mkdir(BOOT_PATH);

    char path[32], tmp[32];
    bootPath(which, path, sizeof(path));
    bootTmpPath(which, tmp, sizeof(tmp));

    File f = LittleFS.open(tmp, "w");
    if (!f) return false;
    const size_t put = f.write(data, len);
    f.close();
    if (put != len) { LittleFS.remove(tmp); return false; }
    if (!LittleFS.rename(tmp, path)) { LittleFS.remove(tmp); return false; }
    return true;
  }

  // PROGMEM variant of writeBootFile, for the compiled-in factory copy
  // (boot_data.h). The staging buffer is why this is a separate function and
  // not writeBootFile with a cast: File::write() dereferences its argument as
  // a RAM pointer, so a PROGMEM array passed through it writes from whatever
  // data-mapped address holds flash — garbage files, not a compiler error.
  // Same tmp-then-rename shape, so a power cut mid-provision still leaves the
  // previous file intact rather than a truncated one.
  bool writeBootFilePgm(uint8_t which, const uint8_t* data, uint16_t len) {
    if (which > 2 || len == 0 || len > BOOT_MAX_BYTES) return false;
    if (!LittleFS.exists(BOOT_PATH)) LittleFS.mkdir(BOOT_PATH);

    char path[32], tmp[32];
    bootPath(which, path, sizeof(path));
    bootTmpPath(which, tmp, sizeof(tmp));

    File f = LittleFS.open(tmp, "w");
    if (!f) return false;
    uint8_t  buf[128];
    uint16_t put = 0;
    bool     ok  = true;
    while (put < len) {
      const uint16_t room = (uint16_t)(len - put);
      const uint16_t n    = room < sizeof(buf) ? room : (uint16_t)sizeof(buf);
      memcpy_P(buf, data + put, n);
      if (f.write(buf, n) != n) { ok = false; break; }
      put = (uint16_t)(put + n);
    }
    f.close();
    if (!ok) { LittleFS.remove(tmp); return false; }
    if (!LittleFS.rename(tmp, path)) { LittleFS.remove(tmp); return false; }
    return true;
  }

private:
  static void slotPath(uint8_t slot, char* out) { snprintf(out, 24, "%s/s%02u.bin", STORE_PATH, (unsigned)slot); }
  static void tmpPath (uint8_t slot, char* out) { snprintf(out, 24, "%s/s%02u.tmp", STORE_PATH, (unsigned)slot); }

  // Built from the BOOT_PATH macro rather than a literal "/boot", so the path
  // lives in exactly one place. Two spellings of a directory is how a file ends
  // up written somewhere nothing reads.
  static void bootPath(uint8_t which, char* out, size_t cap) {
    const char* leaf = which == BOOT_TARGET_SEGS ? "segs.bin"
                     : which == BOOT_TARGET_TRIS ? "tris.bin"
                                                 : "meta.json";
    snprintf(out, cap, "%s/%s", BOOT_PATH, leaf);
  }
  static void bootTmpPath(uint8_t which, char* out, size_t cap) {
    char base[32];
    bootPath(which, base, sizeof(base));
    snprintf(out, cap, "%s.tmp", base);
  }

  static constexpr const char* NS = "cchud";
  Preferences prefs_;
};
