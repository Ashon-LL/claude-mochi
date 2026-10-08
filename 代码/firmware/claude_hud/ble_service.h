// ble_service.h — GATT server: RX (host->device writes) and TX (notifications).
//
// Uses the ESP32 core's bundled BLEDevice rather than NimBLE. NimBLE shines on
// chips with tight RAM, but this firmware keeps no framebuffer (dirty-rect
// rendering), so memory pressure is already small — and the bundled stack is
// proven on this exact board, which is one fewer library to install.
#pragma once
#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include "config.h"
#include "protocol.h"

// Frames arrive as a stream, not as messages: an ATT write can carry a partial
// frame, and two frames can share one write. So RX is buffered and drained.
// The buffer is deliberately larger than any legal frame so a well-formed
// sequence never needs to be split.
#define RX_BUF 512

class BleService {
public:
  using FrameFn = void (*)(const Frame& f);

  void begin(const char* name, FrameFn onFrame) {
    onFrame_ = onFrame;

    BLEDevice::init(name);
    // Our proposal; the peer's own request decides the final value. Until
    // negotiation finishes the stack is stuck at 23 bytes, which is why every
    // frame path has to work with a 17-byte payload.
    BLEDevice::setMTU(BLE_MTU_WANTED + 7);

    pServer_ = BLEDevice::createServer();
    pServer_->setCallbacks(new ServerCB(*this));

    pService_ = pServer_->createService(BLE_SERVICE_UUID);

    pRx_ = pService_->createCharacteristic(
        BLE_RX_UUID,
        BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
    pRx_->setCallbacks(new RxCB(*this));

    // Without a CCCD descriptor the client's write to the notification-enable
    // handle fails, and notify() then silently does nothing — which looks
    // exactly like "my status frames are never arriving".
    pTx_ = pService_->createCharacteristic(
        BLE_TX_UUID,
        BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
    pTx_->addDescriptor(new BLE2902());

    pService_->start();

    // The service UUID must be in the advertising payload, or a central that
    // filters by service (the correct way to scan) never sees this device even
    // though it is advertising its name loud and clear. This was dropped during
    // the rewrite and cost a whole debugging round.
    BLEAdvertising* pAdv = BLEDevice::getAdvertising();
    pAdv->addServiceUUID(BLE_SERVICE_UUID);
    pAdv->setName(name);
    pAdv->setScanResponse(true);
    BLEDevice::startAdvertising();
  }

  bool connected() const  { return connected_; }
  // subscribed() is declared lower down, with the note on why send() must not
  // gate on it.
  uint16_t mtu() const    { return mtu_; }

  // Must be called from loop(): refreshes whether the host has notifications
  // enabled on TX. Purely advisory — see the note on subscribed() — so a
  // failure here costs accuracy of a status readout, not the status itself.
  void poll() {
    if (!connected_ || !pTx_) return;

    // BLEUUID, not a bare uint16_t: that overload does not exist on this BLE
    // library, and the implicit conversion it silently chose produced a
    // descriptor lookup that never matched.
    BLEDescriptor* cccd = pTx_->getDescriptorByUUID(BLEUUID((uint16_t)0x2902));
    if (!cccd) return;

    // getValue() is a raw pointer here, not a std::string.
    const uint8_t* v = cccd->getValue();
    const size_t   n = cccd->getLength();
    const bool on = (n >= 2) && (v[0] & 0x01);
    if (on != subscribed_) subscribed_ = on;
  }

  // Sends a device->host frame. Returns false when it was dropped because the
  // link is down — callers that care (PONG) can retry later.
  bool send(uint8_t type, uint8_t seq, const uint8_t* payload, uint16_t len) {
    if (!connected_ || !pTx_ || len > FRAME_MAX_PAYLOAD) return false;

    uint8_t buf[FRAME_HDR + FRAME_MAX_PAYLOAD + 1];
    const uint16_t n = frameEncode(type, seq, payload, len, buf, sizeof(buf));
    if (n == 0) return false;
    pTx_->setValue(buf, n);
    pTx_->notify();
    return true;
  }

  // Advisory only: whether the host currently has notifications enabled. It is
  // reported to the host and shown in the UI, but send() does NOT gate on it.
  //
  // An earlier revision gated every send() on this flag being set, and the
  // check could not find the CCCD descriptor (this BLE library exposes
  // getDescriptorByUUID(const char*) and getDescriptorByUUID(BLEUUID) only,
  // not a uint16_t overload), so the flag never became true and every
  // device-to-host frame — STATUS, PONG, ACK, the boot log — was silently
  // dropped. The host saw a perfectly healthy downlink and zero uplink.
  //
  // The asymmetry is the whole point: a false "subscribed" costs one dropped
  // notify(), a false "not subscribed" costs the entire status channel.
  bool subscribed() const { return subscribed_; }

  void ack(uint8_t ackedType, uint8_t ackedSeq, uint8_t code) {
    const uint8_t pl[3] = { ackedType, ackedSeq, code };
    send(MSG_ACK, 0, pl, sizeof(pl));
  }

private:
  struct ServerCB : public BLEServerCallbacks {
    explicit ServerCB(BleService& s) : self(s) {}

    // Single-argument form only: this BLE version exposes no MTU parameter,
    // and BLEDevice::getMTU() is static so there is nowhere to cache it per
    // connection. The effective MTU is whatever negotiation ended at; we do
    // not need to know it, since frames are chunked by the sender.
    void onConnect(BLEServer*) override {
      self.connected_ = true;
      self.mtu_ = BLEDevice::getMTU();
    }
    void onDisconnect(BLEServer*) override {
      self.connected_ = false;
      self.subscribed_ = false;
      BLEDevice::startAdvertising();   // resume advertising or the UI cannot rescan
    }
    BleService& self;
  };

  struct RxCB : public BLECharacteristicCallbacks {
    explicit RxCB(BleService& s) : self(s) {}

    void onWrite(BLECharacteristic* c) override {
      const uint8_t* d = c->getData();
      const size_t   n = c->getLength();
      if (!d || n == 0) return;
      self.push_(d, (uint16_t)n);
      self.drain_();
    }

    void onRead(BLECharacteristic*) override {}   // TX is write-only in practice
    BleService& self;
  };

  void push_(const uint8_t* d, uint16_t n) {
    if (n > RX_BUF - len_) {
      // The peer is sending faster than we parse. Drop the oldest byte rather
      // than the newest: a partial frame is dropped by the caller anyway, and
      // dropping the tail breaks resynchronisation for the next frame.
      len_ = (uint16_t)(RX_BUF - n);
    }
    memcpy(buf_ + len_, d, n);
    len_ = (uint16_t)(len_ + n);
  }

  void drain_() {
    for (;;) {
      Frame f;
      const int16_t used = frameDecode(buf_, len_, f);

      if (used == FP_NEED_MORE) return;
      if (used == FP_BAD) {
        // Not a frame start; drop one byte and try again. This is what makes
        // the link self-healing after a corrupted chunk.
        memmove(buf_, buf_ + 1, (size_t)--len_);
        continue;
      }

      onFrame_(f);
      memmove(buf_, buf_ + used, (size_t)(len_ - used));
      len_ = (uint16_t)(len_ - used);
      if (len_ == 0) return;
    }
  }

  FrameFn           onFrame_    = nullptr;
  BLEServer*        pServer_    = nullptr;
  BLEService*       pService_   = nullptr;
  BLECharacteristic* pRx_        = nullptr;
  BLECharacteristic* pTx_        = nullptr;

  bool     connected_  = false;
  bool     subscribed_ = false;
  uint16_t mtu_        = 23;   // ATT default; a floor, not a choice

  uint8_t  buf_[RX_BUF];
  uint16_t len_ = 0;
};
