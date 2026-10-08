"""clean_slots.py — remove duplicate slot assignments and re-establish bindings.

The device has 12 slots and no notion of "the same expression twice". Early
uploads each claimed a fresh slot, so five slots ended up holding two distinct
expressions. This keeps one slot per expression id and rebinds the states that
the delete endpoint unbound.

Run while the daemon is up:

    python tools/clean_slots.py
"""

from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:17321"

# slot -> keep? Anything not listed here is deleted.
KEEP_SLOTS = (0, 2)

# What to bind once the dust settles: expression id -> states.
REBIND = {"custom": ["thinking"]}


def call(method: str, path: str, body: dict | None = None) -> dict | None:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  {method} {path} failed: {e}")
        return None


def main() -> int:
    ex = call("GET", "/expressions")
    if ex is None:
        print("daemon unreachable — start it first")
        return 1

    slots = {int(k): v for k, v in ex.get("slots", {}).items()}
    print(f"before: {len(slots)} slots -> {sorted(slots)}")

    doomed = [s for s in sorted(slots) if s not in KEEP_SLOTS]
    for slot in doomed:
        result = call("DELETE", f"/expressions/{slot}")
        verdict = "ok" if result and result.get("ok") else "FAILED"
        print(f"  delete slot {slot:<2} ({slots[slot].get('id')}): {verdict}")

    # The delete endpoint unbinds every state, including ones that were pointing
    # at a slot we kept. Re-establish what should be bound.
    for expr_id, states in REBIND.items():
        item = call("GET", f"/expressions/item/{expr_id}")
        if item is None:
            print(f"  cannot rebind '{expr_id}': not in the library")
            continue
        result = call("POST", "/expressions/upload",
                      {"id": expr_id, "expression": item["expression"],
                       "states": states})
        if result and result.get("ok"):
            bound = ",".join(b["state"] for b in result.get("bound", []))
            print(f"  rebind '{expr_id}' -> slot {result['slot']} ({bound})")
        else:
            print(f"  rebind '{expr_id}' FAILED: {result}")

    after = call("GET", "/expressions")
    if after is not None:
        slots2 = {int(k): v for k, v in after.get("slots", {}).items()}
        print(f"\nafter: {len(slots2)} slots -> {sorted(slots2)}")
        for slot, meta in sorted(slots2.items()):
            print(f"  slot {slot:>2}  {meta.get('id'):<10} {meta.get('bytes')}B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
