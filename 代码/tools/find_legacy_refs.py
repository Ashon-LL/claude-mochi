"""Find everything that still points at the legacy daemon tree.

Renaming a directory that something still references produces a failure that is
silent — a path that no longer resolves, reported as nothing. So search first,
rename second.

    python tools\\find_legacy_refs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(r"D:\Claude DIY")
LEGACY_DIR = ROOT / "daemon"

# Anything that names the old daemon by path, or the stale frozen build that
# came out of it.
PROBES = (
    "daemon\\hud_daemon.py",
    "daemon/hud_daemon.py",
    "release\\bin",
    "release/bin",
    "hud_api.exe",
    "hud_daemon.exe",
)

SKIP_DIRS = {"node_modules", "__pycache__", "release_new", "dist", "build_pyi",
             ".git", ".vscode", "win-unpacked", "test_portable"}

TEXT_SUFFIX = {".py", ".js", ".json", ".md", ".ps1", ".txt", ".yml", ".yaml",
               ".tsx", ".ts", ".ino", ".h", ".csproj", ".cs", ".cmd", ".bat"}


def main() -> int:
    if not LEGACY_DIR.is_dir():
        print(f"legacy dir not found (already renamed?): {LEGACY_DIR}")
        return 0

    print(f"legacy daemon tree: {LEGACY_DIR}")
    total = sum(1 for p in LEGACY_DIR.rglob("*") if p.is_file())
    print(f"  {total} files, already excluded from the running system\n")

    hits: list[tuple[Path, int, str]] = []
    scanned = 0
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIX:
            continue
        # Skip the legacy tree's own contents: it is what we are renaming.
        try:
            path.relative_to(LEGACY_DIR)
            continue
        except ValueError:
            pass
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for probe in PROBES:
                if probe in line:
                    hits.append((path, i, line.strip()[:120]))
                    break

    print(f"scanned {scanned} text files outside the legacy tree\n")
    if not hits:
        print("NO REFERENCES. Renaming is safe.")
        return 0

    print(f"{len(hits)} REFERENCE(S):")
    for path, line_no, text in hits:
        print(f"  {path.relative_to(ROOT)}:{line_no}")
        print(f"      {text}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
