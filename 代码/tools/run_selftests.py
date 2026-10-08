"""Run every daemon module's self-test in one interpreter.

Modules share state through logbus's module-level logger, so running each one in
its own process would be slower and would hide cross-module breakage. One
process, each selftest called in turn, failures reported at the end.

    python tools\run_selftests.py
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "daemon"))

MODULES = [
    "protocol",
    "state_map",
    "config",
    "device",
    "expressions",
    "paths",
    "boot",
    "settings_patch",
    "settings_watch",
    "ble_link",
    "ipc_server",
]

failures: list[str] = []
skipped: list[str] = []

for name in MODULES:
    try:
        mod = __import__(f"hud_daemon.{name}", fromlist=["_selftest"])
    except Exception as exc:
        failures.append(f"{name}: import failed — {type(exc).__name__}: {exc}")
        traceback.print_exc()
        continue

    selftest = getattr(mod, "_selftest", None)
    if selftest is None:
        skipped.append(f"{name}: no _selftest")
        continue
    try:
        selftest()
    except Exception as exc:
        failures.append(f"{name}: {type(exc).__name__}: {exc}")
        traceback.print_exc()

print("\n=== summary ===")
if skipped:
    for line in skipped:
        print(f"SKIP  {line}")
if failures:
    for line in failures:
        print(f"FAIL  {line}")
    print(f"\n{len(MODULES) - len(failures)}/{len(MODULES)} passed")
    raise SystemExit(1)
print(f"{len(MODULES)}/{len(MODULES)} passed")
