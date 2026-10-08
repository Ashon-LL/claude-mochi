"""scan_probe.py — standalone BLE scan, no daemon logic.

Answers one question: is the device advertising at all, and if so under what
name and with which service UUIDs? The daemon's scan filters on the service
UUID, so a device that advertises a bare name and no UUIDs is invisible to it
while still being perfectly visible here.

    python tools/scan_probe.py [seconds]
"""

from __future__ import annotations

import asyncio
import sys

from bleak import BleakScanner

SERVICE_UUID = "12345678-1234-1234-1234-123456789abc"


async def probe(seconds: float) -> int:
    print(f"[*] scanning {seconds:.0f}s for anything advertising...")
    devices = await BleakScanner.discover(timeout=seconds, return_adv=True)
    print(f"[*] {len(devices)} device(s) seen\n")

    target_lower = SERVICE_UUID.lower()
    found = False
    for address, (device, adv) in sorted(devices.items()):
        names = adv.local_name or device.name or "(unnamed)"
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        has_ours = target_lower in uuids
        marker = "  <== OUR DEVICE" if has_ours else ""
        print(f"{address}  rssi={adv.rssi}")
        print(f"    name       : {names}{marker}")
        print(f"    uuids      : {uuids if uuids else '(none)'}")
        if adv.manufacturer_data:
            print(f"    manufacturer: {dict(adv.manufacturer_data)}")
        print()
        if has_ours:
            found = True

    if found:
        print("[OK] our service UUID is being advertised.")
    else:
        print("[FAIL] our service UUID was NOT advertised by anything.")
        print("       Any device whose name contains 'Claude' or 'HUD' above?")
    return 0 if found else 1


if __name__ == "__main__":
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    try:
        raise SystemExit(asyncio.run(probe(seconds)))
    except KeyboardInterrupt:
        raise SystemExit(0)
