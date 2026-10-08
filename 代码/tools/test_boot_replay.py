"""Replay boot_anim.h's state machine against the real files, on the host.

There is no arduino-cli on this machine, so the C++ cannot be compiled here.
That leaves a real gap: a state machine that drives a boot sequence is exactly
the kind of logic that is easy to get subtly wrong (a phase that never
advances, a file that is read twice, a hold that never expires) and impossible
to see without running it.

So this re-implements BootAnim's transitions in Python from the same meta.json
and the same .bin layout, and replays a whole boot against them. It validates
the *design* — phase order, pacing, file offsets, termination — rather than the
C++ itself. The two are kept in step by the comments in both files, and the
shared fixtures (meta.json, segs.bin, tris.bin) are the real ones, so a
misunderstanding of the file format fails here instead of on the panel.

    python tools\test_boot_replay.py [--bootdir DIR] [--ticks N]
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

# Mirrors BootPhase in boot_anim.h. Order matters: it is the assert below.
BOOT_IDLE, BOOT_TEXT, BOOT_REVEAL, BOOT_FILL, BOOT_HOLD = range(5)
PHASE_NAME = ["idle", "text", "reveal", "fill", "hold"]

DEFAULT_BOOT = Path(__file__).resolve().parent / "boot"


class Boot:
    """A faithful-as-possible port of BootAnim, file layout included."""

    def __init__(self, bootdir: Path):
        self.meta = json.loads((bootdir / "meta.json").read_text(encoding="utf-8"))
        segs = (bootdir / "segs.bin").read_bytes()
        self.segs = [struct.unpack_from("<4h", segs, i)
                     for i in range(0, len(segs) - 7, 8)]
        tris = (bootdir / "tris.bin").read_bytes()
        self.tris = [struct.unpack_from("<6h", tris, i)
                     for i in range(0, len(tris) - 11, 12)]
        self.segments = len(self.segs)
        # The file is the authority on its own size; meta.json's count is
        # allowed to be absent or stale, exactly as loadMeta() treats it.
        self.meta_segments = self.meta.get("segments") or self.segments

        duration = self.meta.get("duration_ms", 1600) or 1600
        self.step_ms = max(1, min(200, duration // max(1, self.segments)))
        self.hold_ms = self.meta.get("hold_ms", 1200)
        self.loop = bool(self.meta.get("loop", False))

        self.phase = BOOT_IDLE
        self.phase_since = 0
        self.last_step = 0
        self.drawn = 0
        self.seg_pos = 0
        self.painted_tris = 0
        self.loops_left = 2 if self.loop else 1
        # An event log, so the test can assert on what actually happened.
        self.events: list[tuple[int, int, str]] = []

    @property
    def available(self) -> bool:
        return self.segments > 0

    def _log(self, now: int, event: str) -> None:
        self.events.append((now, self.phase, event))

    def start(self, now: int) -> None:
        if not self.available:
            return
        self.phase = BOOT_TEXT
        self.phase_since = now
        self.drawn = 0
        self.seg_pos = 0
        self.loops_left = 2 if self.loop else 1
        self._log(now, "start")

    @property
    def active(self) -> bool:
        return self.phase != BOOT_IDLE

    def tick(self, now: int) -> None:
        if self.phase == BOOT_IDLE:
            return
        if self.phase == BOOT_TEXT:
            if now - self.phase_since < 400:
                return
            self.phase = BOOT_REVEAL
            self.phase_since = now
            self.last_step = now
            self.drawn = 0
            self.seg_pos = 0
            self._log(now, "text->reveal")
            return
        if self.phase == BOOT_REVEAL:
            if now - self.last_step < self.step_ms:
                return
            self.last_step = now
            self._draw_one_segment()
            if self.drawn >= self.segments:
                self.phase = BOOT_FILL
                self.phase_since = now
                self._draw_fill()
                self._log(now, "reveal->fill")
            return
        if self.phase == BOOT_FILL:
            if now - self.phase_since < 250:
                return
            self.phase = BOOT_HOLD
            self.phase_since = now
            self._log(now, "fill->hold")
            return
        if self.phase == BOOT_HOLD:
            if now - self.phase_since < self.hold_ms:
                return
            if self.loops_left > 1:
                self.loops_left -= 1
                self.phase = BOOT_TEXT
                self.phase_since = now
                self.seg_pos = 0
                self.drawn = 0
                self._log(now, "loop")
                return
            self.phase = BOOT_IDLE
            self._log(now, "done")

    def _draw_one_segment(self) -> None:
        if self.seg_pos >= len(self.segs):
            return
        self.segs[self.seg_pos]
        self.seg_pos += 1
        self.drawn += 1

    def _draw_fill(self) -> None:
        self.painted_tris = len(self.tris)
        self._log(0, "painted %d triangles" % len(self.tris))


def replay(boot: Boot, ticks: int, tick_ms: int = 4) -> list[tuple[int, int, str]]:
    """Call tick() on every tick_ms and return the event log."""
    boot.start(0)
    now = 0
    for _ in range(ticks):
        boot.tick(now)
        now += tick_ms
    return boot.events


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootdir", default=str(DEFAULT_BOOT))
    ap.add_argument("--ticks", type=int, default=3000)
    ap.add_argument("--tick-ms", type=int, default=4)
    args = ap.parse_args()

    bootdir = Path(args.bootdir)
    if not bootdir.exists():
        print(f"boot dir not found: {bootdir}")
        print("generate it with:")
        print(r'  python tools\mochi_to_boot.py "F:\桌宠代码\clawd-mochi'
              r'\clawd_mochi\clawd_mochi.ino" tools\boot')
        return 2

    b = Boot(bootdir)
    print(f"boot dir     {bootdir}")
    print(f"segments     {b.segments} (meta says {b.meta_segments})")
    print(f"triangles    {len(b.tris)}")
    print(f"step         {b.step_ms} ms   hold {b.hold_ms} ms   loop {b.loop}")

    problems: list[str] = []

    # ── file-format checks, the ones that would look like a garbled logo ──────
    if b.segments * 8 != (bootdir / "segs.bin").stat().st_size:
        problems.append("segs.bin size is not a multiple of 8")
    if len(b.tris) * 12 != (bootdir / "tris.bin").stat().st_size:
        problems.append("tris.bin size is not a multiple of 12")
    for x1, y1, x2, y2 in b.segs:
        if not (0 <= x1 <= 240 and 0 <= y1 <= 240
                and 0 <= x2 <= 240 and 0 <= y2 <= 240):
            problems.append(f"segment outside the panel: {x1},{y1} -> {x2},{y2}")
            break
    for t in b.tris:
        if any(not (0 <= v <= 240) for v in t):
            problems.append(f"triangle outside the panel: {t}")
            break

    if not b.available:
        # A device with no animation must still boot, so this is a legal state —
        # but it is not what this run is testing.
        print("\nno animation available; nothing to replay")
        return 0

    events = replay(b, args.ticks, args.tick_ms)
    names = [e[2] for e in events]

    # ── the sequence must be text -> reveal -> fill -> hold -> done ──────────
    order = [n for n in names if "->" in n or n == "done"]
    expected = ["text->reveal", "reveal->fill", "fill->hold", "done"]
    if order != expected:
        problems.append(f"phase order {order} != {expected}")

    # ── the whole logo must be drawn exactly once ───────────────────────────
    if b.seg_pos != b.segments:
        problems.append(f"drew {b.seg_pos}/{b.segments} segments")
    if b.painted_tris != len(b.tris):
        problems.append(f"painted {b.painted_tris}/{len(b.tris)} triangles")

    # ── it must terminate, and not be so slow that boot looks hung ───────────
    done_at = next((now for now, _, n in events if n == "done"), None)
    if done_at is None:
        problems.append(f"did not finish within {args.ticks * args.tick_ms} ms")
    else:
        total = 400 + b.segments * b.step_ms + 250 + b.hold_ms
        print(f"finished at  {done_at} ms (expected about {total} ms)")
        if done_at > 15000:
            problems.append(f"boot takes {done_at} ms; too long before BLE starts")

    # ── no two segments drawn on the same tick ─────────────────────────────
    # A fresh instance that is actually started: the one replay() consumed is
    # already at its end state, so its counters read zero for everything, and a
    # never-started instance returns immediately from tick() and draws nothing
    # at all. Both would report a clean run for an empty animation.
    pacing = Boot(bootdir)
    pacing.start(0)
    ticks_per_seg: dict[int, int] = {}
    drawn = 0
    now = 0
    for _ in range(args.ticks):
        before = pacing.seg_pos
        pacing.tick(now)
        if pacing.seg_pos == before + 1:
            ticks_per_seg[now // pacing.step_ms] = \
                ticks_per_seg.get(now // pacing.step_ms, 0) + 1
            drawn += 1
        now += args.tick_ms
    if drawn != b.segments:
        problems.append(f"pacing run drew {drawn}/{b.segments} segments")
    if any(v > 1 for v in ticks_per_seg.values()):
        problems.append("more than one segment drawn per step")

    print(f"pacing      {'one segment per step' if drawn == b.segments else 'BROKEN'}"
          f"  ({drawn}/{b.segments} segments, one per {pacing.step_ms} ms)")

    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nboot replay OK: phases, pacing, file offsets and termination all check out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
