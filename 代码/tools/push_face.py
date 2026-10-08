"""Upload a face JSON to the panel and bind it to one or more states.

The expression editor already does this over HTTP; this is the same call from
the command line, so a face can be iterated on in a text editor and pushed
without opening the panel. Useful precisely because effects are data: the
animation lives in the JSON, not in the firmware.

    python tools\\push_face.py <face.json> --states idle [--url ...]
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

DEFAULT_URL = "127.0.0.1:17321"
VALID_STATES = ("idle", "thinking", "tool_start", "tool_end",
                "waiting", "error", "offline")
MAX_PRIMS = 16
MAX_BYTES = 4096

# The firmware's Fx enum. The editor and DESIGN.md write effect as a name
# ("blink"); the device reads it as this index. Accepting either here means the
# check does not reject a file the firmware would take.
EFFECTS_BY_NAME = ("none", "blink", "pulse", "shake", "spin", "fade")
VALID_EFFECTS = tuple([*EFFECTS_BY_NAME, *range(len(EFFECTS_BY_NAME))])


def effect_code(fx):
    """Normalise a layer's effect to the numeric index the firmware wants."""
    if isinstance(fx, bool):
        return None
    if isinstance(fx, int):
        return fx if 0 <= fx < len(EFFECTS_BY_NAME) else None
    if isinstance(fx, str):
        return EFFECTS_BY_NAME.index(fx) if fx in EFFECTS_BY_NAME else None
    return None


def request(host: str, port: int, method: str, path: str,
            body: dict | None = None, timeout: float = 30.0):
    """Raw-socket HTTP. urlopen honours the system proxy, and a loopback request
    routed through one fails in a way that looks like the daemon never ran."""
    payload = json.dumps(body).encode() if body is not None else b""
    # Built before the socket opens so a malformed request never costs a
    # connection, and so the paren nesting is readable.
    request_text = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Connection: close\r\n"
        + (f"Content-Length: {len(payload)}\r\n"
           "Content-Type: application/json\r\n" if payload else "")
        + "\r\n"
    )
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.sendall(request_text.encode() + payload)
        chunks = []
        while True:
            c = sock.recv(65536)
            if not c:
                break
            chunks.append(c)
    finally:
        sock.close()
    raw = b"".join(chunks)
    head, _, resp = raw.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, (json.loads(resp.decode()) if resp.strip() else {})


def check_face(face: dict) -> list[str]:
    """Problems that would make the panel reject it, found before sending."""
    problems: list[str] = []
    layers = face.get("layers")
    if not isinstance(layers, list) or not layers:
        problems.append("no layers")
        return problems
    if len(layers) > MAX_PRIMS:
        problems.append(f"{len(layers)} layers, firmware allows {MAX_PRIMS}")

    blob = json.dumps(face, separators=(",", ":")).encode()
    if len(blob) > MAX_BYTES:
        problems.append(f"{len(blob)} bytes, firmware allows {MAX_BYTES}")

    for i, layer in enumerate(layers):
        kind = layer.get("type")
        if kind not in ("rect", "circle", "line", "poly", "text"):
            problems.append(f"layer {i}: unknown type {kind!r}")
            continue
        # An effect the firmware cannot resolve becomes FX_NONE, so the layer is
        # silently static. Caught here rather than on the panel, where it looks
        # identical to "the animation is too subtle to see".
        code = effect_code(layer.get("effect", "none"))
        if code is None:
            problems.append(
                f"layer {i}: effect {layer.get('effect')!r} is not one of "
                f"{EFFECTS_BY_NAME} (or its index 0..{len(EFFECTS_BY_NAME) - 1})")
        elif code != 0:
            if not isinstance(layer.get("period_ms"), int) or layer["period_ms"] <= 0:
                problems.append(
                    f"layer {i}: effect {EFFECTS_BY_NAME[code]!r} needs a "
                    f"positive period_ms, got {layer.get('period_ms')!r}")
            # blink is the one effect whose own span has a ceiling: at or above
            # the cycle the layer is permanently visible and stops blinking.
            if code == 1:
                on_ms = layer.get("on_ms")
                period = layer.get("period_ms", 0)
                if not isinstance(on_ms, int) or on_ms <= 0:
                    problems.append(f"layer {i}: blink needs a positive on_ms")
                elif isinstance(period, int) and on_ms >= period:
                    problems.append(
                        f"layer {i}: on_ms {on_ms} >= period {period} — the "
                        f"layer would be permanently visible, i.e. no blink")
            if not 0 <= (layer.get("amount", 100) or 100) <= 30000:
                problems.append(f"layer {i}: amount out of a sane range")
        if kind == "poly":
            pts = layer.get("points") or []
            if len(pts) < 3:
                problems.append(f"layer {i}: poly needs >= 3 points")
            if len(pts) > 16:
                problems.append(f"layer {i}: poly allows 16 points")
        if kind == "text" and len(layer.get("text", "")) > 24:
            problems.append(f"layer {i}: text allows 24 chars")
        # Coordinates the panel cannot show are silently clipped, which reads as
        # a face that is broken for no visible reason.
        for key in ("x", "y", "cx", "cy", "x2", "y2"):
            if isinstance(layer.get(key), int) and not 0 <= layer[key] <= 240:
                problems.append(f"layer {i}: {key}={layer[key]} outside 0..240")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("face", nargs="?", default=None,
                    help="path to the face JSON; omit for the demo face")
    ap.add_argument("--states", default="idle",
                    help="comma-separated states to bind to")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--preview", action="store_true",
                    help="print the layer summary and exit without uploading")
    args = ap.parse_args()

    path = Path(args.face) if args.face else (
        Path(__file__).resolve().parent / "faces" / "idle_alive.json")
    if not path.exists():
        print(f"not found: {path}")
        return 2
    try:
        face = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read face: {exc}")
        return 2

    problems = check_face(face)
    layers = face.get("layers", [])
    # Names for display, indices on the wire: a layer is animated when its effect
    # resolves to anything but 0.
    animated = [(l, effect_code(l.get("effect", "none"))) for l in layers]
    animated = [(l, c) for l, c in animated if c not in (None, 0)]
    print(f"face        {path.name}")
    print(f"  name      {face.get('name', '(unnamed)')}")
    print(f"  layers    {len(layers)}  ({len(animated)} animated)")
    for l, code in animated:
        print(f"    {l['type']:<7} {EFFECTS_BY_NAME[code]:<6} "
              f"period={l.get('period_ms', 0)}ms amount={l.get('amount', 100)}")
    print(f"  size      {len(json.dumps(face, separators=(',', ':')))} bytes")

    if problems:
        print("\nPROBLEMS (the panel would reject or clip these):")
        for p in problems:
            print(f"  - {p}")
        return 1

    if args.preview:
        return 0

    states = [s.strip() for s in args.states.split(",") if s.strip()]
    bad = [s for s in states if s not in VALID_STATES]
    if bad:
        print(f"\nunknown state(s): {', '.join(bad)}")
        print(f"valid: {', '.join(VALID_STATES)}")
        return 1

    host, _, port_s = args.url.partition(":")
    port = int(port_s or 17321)
    status, body = request(host, port, "POST", "/expressions/upload",
                           {"expression": face, "states": states})
    if status != 200 or not body.get("ok"):
        print(f"\nupload failed: {body.get('error', status)}")
        return 1

    bound = ", ".join(b["state"] for b in body.get("bound", []))
    rejected = body.get("rejected") or []
    print(f"\nuploaded to slot {body.get('slot')} ({body.get('bytes')} bytes)")
    print(f"  bound: {bound or 'nothing'}")
    if rejected:
        # The device refused these. Until now the response would have claimed
        # success; the bound list is what actually took.
        print(f"  REJECTED by the device: {', '.join(rejected)}")
        return 1
    print("\nthe panel is showing it now — that state's face is live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
