"""audit_ui.py — what the daemon can do vs what the UI can reach.

A capability the daemon has but the window does not is a capability the user
does not have. This diffs the daemon's HTTP surface against the paths the
renderer actually fetches, so the gap is a fact rather than an opinion.

    python tools/audit_ui.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent           # ...\Claude DIY\代码
DAEMON_SRC = ROOT / "daemon" / "hud_daemon" / "ipc_server.py"

# The Electron app is a SIBLING of this root, not a child:
# D:\Claude DIY\electron sits next to D:\Claude DIY\代码. Getting this wrong has
# caused three separate bugs, so it is called out rather than assumed.
UI_SRC = ROOT.parent / "electron" / "src"

# Routes that exist for machine-to-machine use and legitimately have no UI.
NO_UI_BY_DESIGN = {
    "POST /hook": "Claude Code's hook shim calls this directly",
    "GET /expressions/test": "one-shot diagnostic, superseded by the editor",
}


def daemon_routes() -> list[str]:
    src = DAEMON_SRC.read_text(encoding="utf-8")
    return [f"{m.upper():6} {p}" for m, p in
            re.findall(r'@app\.(get|post|delete|put)\("([^"]+)"', src)]


def ui_paths() -> set[str]:
    """Every path the renderer builds from the daemon's base URL."""
    found: set[str] = set()
    files = sorted(UI_SRC.rglob("*.tsx")) + sorted(UI_SRC.rglob("*.ts"))
    for path in files:
        src = path.read_text(encoding="utf-8")
        for match in re.findall(
            r"\$\{(?:DAEMON|window\.claudeHUD\.daemonUrl)\}([^`\"']*)", src
        ):
            found.add(match)
    return found


def normalise(p: str) -> str:
    """Collapse a path parameter to a marker so shapes can be compared.

    Deliberately plain string substitution, not a compiled regex: a template
    literal like ${encodeURIComponent(id)} is not a valid pattern, which is how
    the first version of this script crashed.
    """
    return re.sub(r"\{[^}]*\}|\$\{[^}]*\}", "#", p).rstrip("/")


def main() -> int:
    routes = daemon_routes()
    ui = {p for p in ui_paths() if p.startswith("/")}
    ui_norms = {normalise(p) for p in ui}

    print("daemon routes:")
    for r in routes:
        print(f"  {r}")

    print("\nrenderer fetches:")
    for p in sorted(ui):
        print(f"  {p}")

    gaps = []
    for r in routes:
        method, path = r.split(None, 1)
        key = f"{method} {path}"
        if key in NO_UI_BY_DESIGN:
            continue
        shape = normalise(path)
        if not any(shape == u or shape.startswith(u + "/") for u in ui_norms):
            gaps.append(key)

    print("\nreachable by the daemon but NOT by the UI:")
    print("  (none)" if not gaps else "")
    for g in gaps:
        print(f"  {g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
