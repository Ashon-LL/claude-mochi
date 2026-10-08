"""query_daemon.py — one-shot HTTP probe of the running daemon.

Used to check slot state without the daemon's own console in the way. curl in
this shell keeps swallowing its output, and Python's urllib gives a real
exception when nothing is listening instead of an empty success.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:17321"


def get(path: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=4) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} for {path}: {e.read().decode('utf-8', 'replace')}")
        return None
    except Exception as e:
        print(f"  unreachable ({type(e).__name__}) for {path}")
        return None


def main() -> int:
    st = get("/status")
    if st is None:
        print("NO DAEMON on", BASE)
        return 1

    print("daemon up")
    link = st.get("link", {})
    print(f"  link   : {link.get('state')}  mtu={link.get('mtu')}  "
          f"rtt={link.get('rtt_ms')}ms  addr={link.get('address')}")
    print(f"  state  : {st.get('state')}   events={st.get('events_seen')}  "
          f"udp={st.get('udp_packets')}  dropped={st.get('dropped')}")

    ex = get("/expressions")
    if ex is None:
        return 1

    slots = ex.get("slots", {})
    print(f"\n  slots on device: {len(slots)}/12")
    for slot, meta in sorted(slots.items(), key=lambda kv: int(kv[0])):
        print(f"    slot {slot:>2}  {meta.get('name', '?'):<12} "
              f"id={meta.get('id'):<10} {meta.get('bytes')}B")

    lib = ex.get("library", [])
    print(f"\n  local library: {len(lib)}")
    for e in lib:
        print(f"    {e.get('id'):<12} {e.get('bytes')}B")

    print(f"\n  mtu={ex.get('mtu')} chunk budget={ex.get('chunk_budget')} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
