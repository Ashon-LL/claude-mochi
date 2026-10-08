# PyInstaller spec for the Claude HUD daemon.
#
# Build from the daemon package's parent so the package name is importable:
#
#     cd /d D:\Claude DIY\代码\daemon
#     pyinstaller --noconfirm --clean hud_daemon.spec
#
# onedir, not onefile. A onefile build extracts itself to a temp directory on
# every launch, which costs seconds on a daemon whose whole job is to be already
# running, and gets flagged by security software far more often. The daemon is
# spawned by the Electron app and lives next to it, so onedir's folder is the
# right shape.
#
# What has to be collected by hand:
#
#   bleak        its WinRT backend is loaded dynamically and PyInstaller cannot
#                see it. Without these the daemon starts, then fails at the
#                first BLE call with AttributeError on winrt modules — which
#                looks like a broken driver on the user's machine.
#   uvicorn      dynamic import of the http implementation ("uvicorn.protocols
#                .http.auto" and friends)
#   anyio        bleak 0.22+ is anyio-based, so the asyncio backend is dynamic
#
# The .NET-native hook shim (cchud-hook.exe) is NOT a Python dependency. It is
# shipped next to the frozen daemon by the Electron build's extraResources, and
# paths.py finds it there. It is deliberately absent from this spec.
# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# Paths are resolved from the spec file's own location, which PyInstaller
# exposes as SPECPATH. This spec lives at 代码\daemon\hud_daemon.spec, so the
# package is ./hud_daemon and the project root is one level above that.
#
# SPECPATH is the only one that is safe to reference: a spec file is executed by
# PyInstaller with no __file__ in its namespace, so using it as a fallback in
# the same expression still raises NameError — the fallback is evaluated
# whether or not it is needed.
SPEC_DIR = Path(SPECPATH).resolve()
PACKAGE_DIR = SPEC_DIR / "hud_daemon"          # ...\代码\daemon\hud_daemon
PROJECT_ROOT = SPEC_DIR.parent                 # ...\代码

hidden = []
hidden += collect_submodules("bleak")
# bleak's real backend is chosen by platform; on Windows it is winrt.
hidden += collect_submodules("winrt")
hidden += collect_submodules("uvicorn")
hidden += collect_submodules("anyio")

# bleak ships a .pyd / dll pair under its winrt backend on some installs; data
# files catch anything else the package needs at runtime.
datas = []
try:
    datas += collect_data_files("bleak")
except Exception:
    pass

a = Analysis(
    # The entry point must NOT be the package's __main__.py: frozen, it becomes
    # a top-level script with no package, and its relative imports
    # (`from . import protocol`) all fail. hud_daemon_entry.py imports the
    # package by name instead, so the package keeps its identity.
    [str(SPEC_DIR / "hud_daemon_entry.py")],
    pathex=[str(SPEC_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # One module, one entry point: the console launcher is what the Electron app
    # spawns, and it must not flash a window on Windows.
    excludes=[
        "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
        "matplotlib", "numpy", "pandas", "scipy",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="hud_daemon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # the daemon's console output goes to the log file
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="hud_daemon",
)
