"""bench_shim.py — compare the compiled and Python hook shims fairly.

The number that matters is the cost Claude Code pays per tool call, which is
the whole-process cost, not the time spent inside the shim. The Python version's
own UDP send measures ~1 ms, but spawning a Python interpreter to get there
costs far more, so both must be measured the same way: as a subprocess.

    python tools/bench_shim.py [runs]
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "hookshim" / "cchud-hook-cs" / "bin" / "Release" / "net8.0" / "win-x64" / "publish" / "cchud-hook.exe"
PYTHON = sys.executable
PY = ROOT / "hookshim" / "cchud_hook.py"

PAYLOAD = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                      "session_id": "bench"})


def bench(cmd: list[str], runs: int) -> list[float]:
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        subprocess.run(cmd, input=PAYLOAD, capture_output=True, text=True, timeout=20)
        times.append((time.perf_counter() - start) * 1000.0)
    return times


def main(argv: list[str]) -> int:
    runs = int(argv[1]) if len(argv) > 1 else 8
    if not EXE.exists():
        print(f"[FAIL] {EXE} not found — run dotnet publish first")
        return 1
    if not PY.exists():
        print(f"[FAIL] {PY} not found")
        return 1

    print(f"[*] {runs} runs each, whole-process cost\n")

    # One untimed warm-up each so the first-run antivirus scan does not dominate.
    bench([str(EXE)], 1)
    bench([PYTHON, str(PY)], 1)

    a = bench([str(EXE)], runs)
    b = bench([PYTHON, str(PY)], runs)
    ma, mb = statistics.median(a), statistics.median(b)

    print(f"  {EXE.name:<14} median {ma:6.1f} ms   (min {min(a):6.1f}, max {max(a):6.1f})")
    print(f"  {'cchud_hook.py':<14} median {mb:6.1f} ms   (min {min(b):6.1f}, max {max(b):6.1f})")
    if ma > 0:
        print(f"\n  compiled is {mb / ma:.1f}x faster per tool call")

    print("\n[*] with 20 tool calls in one turn that is "
          f"{(mb - ma) * 20 / 1000.0:.1f} s saved, or none if it is slower.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
