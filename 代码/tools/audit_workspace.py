"""Inventory the workspace and classify each top-level item for a Git upload.

Written because "delete the leftovers" is only safe once the leftovers are
named. The user described the root as residual, but the active frontend
(electron/) and the wiring diagram the firmware comments reference both live
there — so this prints what everything is, what references it, and how big it
is, before anything is removed.

    python tools\\audit_workspace.py
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(r"D:\Claude DIY")

# Things that are definitely not source and never belong in Git.
BUILD_JUNK = {"__pycache__", "node_modules", "release_new", "dist",
              "build_pyi", "win-unpacked", "test_portable", ".git"}

# Name -> what it is, and whether the running system still needs it.
# "dead"    superseded by something under 代码\; nothing live reads it
# "live"    part of the running system right now
# "ask"     referenced by something live, or is the user's own notes
ITEMS = {
    # ── live ─────────────────────────────────────────────────────
    "electron":   ("live", "the Electron UI; the installer is built from it"),
    "接线.xlsx":  ("ask",  "referenced BY NAME in firmware config.h and "
                           "claude_hud.ino ('Wiring per D:\\Claude DIY\\接线.xlsx')"),

    # ── superseded by 代码\ ──────────────────────────────────────
    "claude_hud_ble": ("dead", "first-generation firmware; 代码\\firmware\\"
                                "claude_hud\\ replaced it"),
    "daemon_legacy":  ("dead", "old flat daemon + its __pycache__; renamed from "
                                "daemon\\ after it collided with the package"),
    "server":         ("dead", "old FastAPI on :8765; the daemon's ipc_server.py "
                                "owns that job now"),
    "hooks":          ("dead", "first-generation hook scripts; 代码\\hookshim\\ "
                                "replaced them"),
    "claude_hud_hook.py": ("dead", "first-generation hook"),
    "ble_client.py":  ("dead", "old BLE client, inlined into the legacy daemon"),
    "install.ps1":    ("dead", "installs the legacy tree and writes "
                               "daemon\\hud_daemon.py into settings.json"),
    "uninstall.ps1":  ("dead", "counterpart to the legacy install.ps1"),
    "build":          ("dead", "scripts that build the legacy hud_daemon.exe "
                               "and hud_api.exe"),
    "release":        ("dead", "stale binaries from the legacy tree; the live "
                               "sidecar is 代码\\dist\\hud_daemon\\"),
    "ARCHITECTURE.md": ("dead", "documents the old hook→daemon→8765 design"),
    "QUICKSTART.md":  ("dead", "commands for the legacy tree"),
    "README.md":      ("dead", "describes the legacy three-terminal setup"),

    # ── the user's own scratch notes ─────────────────────────────
    "bk.txt":    ("ask", "your note"),
    "dtest.txt": ("ask", "your note"),
    "el.txt":    ("ask", "your note"),
    "es.txt":    ("ask", "your note"),
    "inj.txt":   ("ask", "your note"),
    "msvc.txt":  ("ask", "your note"),
    "q.txt":     ("ask", "your note", ),
    "qt.txt":    ("ask", "your note"),
}


def tree_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> int:
    print(f"workspace: {ROOT}\n")

    groups: dict[str, list[tuple[str, int, str]]] = {"live": [], "ask": [], "dead": []}
    unlisted: list[str] = []

    for entry in sorted(ROOT.iterdir()):
        name = entry.name
        if name in BUILD_JUNK:
            continue
        if name not in ITEMS:
            unlisted.append(name)
            continue
        kind, why = ITEMS[name]
        size = tree_size(entry) if entry.is_dir() else entry.stat().st_size
        groups[kind].append((name, size, why))

    label = {"live": "LIVE — must not be deleted",
             "ask":  "NEEDS YOUR DECISION",
             "dead": "DEAD — safe to delete"}
    for kind in ("live", "ask", "dead"):
        print(f"── {label[kind]} " + "─" * 40)
        if not groups[kind]:
            print("   (none)")
        for name, size, why in groups[kind]:
            print(f"   {name:<22} {human(size):>10}   {why}")
        print()

    if unlisted:
        print("── UNCLASSIFIED (not in my inventory) " + "─" * 24)
        for name in unlisted:
            p = ROOT / name
            size = tree_size(p) if p.is_dir() else p.stat().st_size
            print(f"   {name:<22} {human(size):>10}")
        print()

    # Build junk is listed separately: it is large, and it is the main reason a
    # Git upload would be slow and noisy.
    print("── BUILD JUNK (also must not go to Git) " + "─" * 20)
    for name in sorted(BUILD_JUNK):
        p = ROOT / name
        if p.exists():
            print(f"   {name:<22} {human(tree_size(p)):>10}")
    for sub in ("electron", "代码"):
        p = ROOT / sub
        if p.is_dir():
            for junk in sorted(BUILD_JUNK):
                q = p / junk
                if q.exists():
                    print(f"   {sub}/{junk:<14} {human(tree_size(q)):>10}")

    # Things inside 代码\ that are machine-specific and must not be committed.
    print("\n── MACHINE-SPECIFIC, inside the live tree " + "─" * 20)
    for rel in ("build.json",):
        p = ROOT / "代码" / rel
        if p.exists():
            print(f"   代码/{rel}  {human(p.stat().st_size):>10}   "
                  f"contains this machine's Python path")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
