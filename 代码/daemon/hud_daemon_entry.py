"""Entry point for the frozen daemon.

PyInstaller builds this file as a top-level script, and a top-level script has
no package for its relative imports to resolve against — `from . import
protocol` raises "attempted relative import with no known parent package" at
start-up, before anything else runs. Importing the package's own __main__ under
its real name keeps every relative import valid for both shapes:

    python -m hud_daemon          # development
    hud_daemon.exe [args]         # frozen, this file

The re-exec below is what makes that work: it imports the package rather than
executing __main__ as a script.
"""
from __future__ import annotations

import asyncio
import sys


def _daemon_argv() -> list[str]:
    """Everything after the exe name, minus the entry script's own name."""
    return sys.argv[1:]


def main() -> int:
    # Imported by name, not executed as a script: the package has to keep its
    # identity for the relative imports throughout it to resolve.
    from hud_daemon.__main__ import main as _daemon_main

    # _daemon_main is async — the daemon is asyncio from the ground up. Calling
    # it without asyncio.run() returns a coroutine object and exits 0, which is
    # the quietest possible failure: a process that starts, does nothing, and
    # reports success.
    result = _daemon_main(_daemon_argv())
    if hasattr(result, "__await__"):
        return asyncio.run(result)
    return result


if __name__ == "__main__":
    # Mirrors hud_daemon/__main__.py's own guard: Ctrl-C must exit 0, not as a
    # traceback, because this is a daemon the Electron app terminates politely.
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
