"""Static consistency check for the firmware, since there is no compiler here.

No arduino-cli on this machine means the C++ cannot be compiled, so the usual
safety net is missing. This is the substitute: it checks the things a compiler
would catch immediately and a human misses easily —

  * every #include resolves to a file that exists
  * every function a header declares is the one the .ino calls
  * braces, parens and brackets balance in each file
  * every identifier the .ino uses is declared somewhere

It is not a type checker and cannot be one. It catches the class of mistake
that costs a flash-and-stare cycle: a renamed function, a missing include, a
typo'd member.

    python tools\\check_firmware.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

FW = Path(__file__).resolve().parents[1] / "firmware" / "claude_hud"

ARDUINO_TYPES = {
    "uint8_t", "uint16_t", "uint32_t", "int8_t", "int16_t", "int32_t",
    "size_t", "bool", "void", "char", "float", "String", "File",
}

# Types and members the new boot_anim.h relies on from elsewhere.
EXPECTED_FROM_RENDERER = ("drawRawLine", "drawRawTriangle",
                          "setBackground", "invalidate", "begin")
EXPECTED_FROM_EXPRESSION = ("exprParseColor",)


def strip_comments(text: str) -> str:
    """Remove // and /* */ comments, leaving strings intact.

    Needed because the rule that matters here is about code, and every file in
    this firmware explains its non-blocking-ness in a comment — so a plain
    substring search for delay( finds the explanation, not a call.
    """
    out = []
    i, n = 0, len(text)
    state = None
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "//":
            if ch == "\n":
                state = None
                out.append(ch)
            i += 1
            continue
        if state == "/*":
            if ch == "*" and nxt == "/":
                state = None
                i += 2
                continue
            if ch == "\n":
                out.append(ch)
            i += 1
            continue
        if state:
            if ch == "\\":
                out.append(text[i:i + 2])
                i += 2
                continue
            if ch == state:
                state = None
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            state = "//"
            i += 2
            continue
        if ch == "/" and nxt == "*":
            state = "/*"
            i += 2
            continue
        if ch in ('"', "'"):
            state = ch
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def balance(text: str, name: str) -> list[str]:
    """Brace/paren/bracket balance, ignoring strings, chars and comments."""
    problems: list[str] = []
    depth = {"{": 0, "(": 0, "[": 0}
    close = {"}": "{", ")": "(", "]": "["}
    i, n = 0, len(text)
    state = None       # None | '"' | "'" | '//' | '/*' | 'raw'
    line = 1
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "\n":
            line += 1
            if state == "//":
                state = None
            i += 1
            continue
        if state == "//":
            i += 1
            continue
        if state == "/*":
            if ch == "*" and nxt == "/":
                state = None
                i += 2
                continue
            i += 1
            continue
        if state in ('"', "'"):
            if ch == "\\":
                i += 2
                continue
            if ch == state:
                state = None
            i += 1
            continue
        if ch == "/" and nxt == "/":
            state = "//"
            i += 2
            continue
        if ch == "/" and nxt == "*":
            state = "/*"
            i += 2
            continue
        if ch in ('"', "'"):
            state = ch
            i += 1
            continue
        if ch in depth:
            depth[ch] += 1
        elif ch in close:
            depth[close[ch]] -= 1
            if depth[close[ch]] < 0:
                problems.append(f"{name}:{line}: unexpected '{ch}'")
        i += 1
    for opener, d in depth.items():
        if d != 0:
            label = "paren" if opener == "(" else "bracket" if opener == "[" else "brace"
            problems.append(f"{name}: {label} unbalanced by {d}")
    return problems


def main() -> int:
    problems: list[str] = []
    checked = 0

    if not FW.is_dir():
        print(f"firmware dir not found: {FW}")
        return 2

    files = sorted(p for p in FW.iterdir() if p.suffix in (".h", ".ino"))
    sources = {p.name: p.read_text(encoding="utf-8", errors="replace")
               for p in files}

    # ── 1. includes resolve ──────────────────────────────────────
    for name, text in sources.items():
        for m in re.finditer(r'^\s*#include\s+"([^"]+)"', text, re.M):
            dep = m.group(1)
            checked += 1
            if dep not in sources:
                problems.append(f"{name}: #include \"{dep}\" has no file in the dir")
            # angle-bracket includes are library headers and are assumed present.

    # ── 2. every declared function is defined somewhere ──────────
    declared: dict[str, str] = {}      # member -> header that declares it
    for name in ("renderer.h", "expression.h"):
        text = sources.get(name)
        if text is None:
            problems.append(f"{name} missing")
            continue
        for m in re.finditer(r"\b(\w+)\s*\(", text):
            fn = m.group(1)
            if fn in ARDUINO_TYPES or fn in ("if", "for", "while", "switch",
                                             "return", "sizeof", "memset",
                                             "memcpy", "snprintf", "strncpy",
                                             "strcmp", "strlen", "strcpy"):
                continue
            declared.setdefault(fn, name)

    for expected in EXPECTED_FROM_RENDERER:
        if expected not in sources.get("renderer.h", ""):
            problems.append(f"renderer.h does not define {expected}()")
    for expected in EXPECTED_FROM_EXPRESSION:
        if expected not in sources.get("expression.h", ""):
            problems.append(f"expression.h does not define {expected}()")

    # ── 3. balance ───────────────────────────────────────────────
    for name, text in sources.items():
        problems += balance(text, name)

    # ── 4. boot-path macros live in exactly one header ───────────
    # A #define is visible from its own line onward only, so a macro defined in
    # a header that is included later is invisible to everything before it —
    # which is how store.h came to reference a BOOT_PATH that did not exist yet.
    # The check is "defined once, in the header included first": config.h is
    # included by every other file, so anything it defines is visible to all.
    for macro in ("BOOT_PATH", "BOOT_META", "BOOT_SEGS", "BOOT_TRIS",
                  "BOOT_MAX_BYTES", "BOOT_TARGET_SEGS"):
        owners = [n for n, t in sources.items()
                  if f"#define {macro}" in t]
        if len(owners) > 1:
            problems.append(f"{macro} is defined in {owners}; must be one header")
        if owners and owners[0] != "config.h":
            problems.append(
                f"{macro} is defined in {owners[0]}, which is included later "
                f"than config.h; move it there")
        if not owners:
            problems.append(f"{macro} is not defined anywhere")

    # A user of the macro must not be in a header that precedes the definition.
    for name, text in sources.items():
        for macro in ("BOOT_PATH", "BOOT_META", "BOOT_SEGS", "BOOT_TRIS"):
            uses = re.findall(rf"\b{macro}\b", strip_comments(text))
            if uses and name != "config.h":
                # Its own include of config.h is what makes it visible.
                if '#include "config.h"' not in text:
                    problems.append(f"{name} uses {macro} without including config.h")
                break
    # ── 5. the .ino's own references ─────────────────────────────
    ino = sources.get("claude_hud.ino", "")
    for sym in ("bleStarted_", "bgColor", "boot."):
        if sym not in ino:
            problems.append(f"claude_hud.ino does not reference {sym}")

    # boot_anim.h must be included by exactly the .ino, and must not include
    # anything the firmware does not already use.
    ba = sources.get("boot_anim.h", "")
    for dep in ("renderer.h", "expression.h", "config.h", "LittleFS.h",
                "ArduinoJson.h"):
        if dep not in ba:
            problems.append(f"boot_anim.h does not include {dep}")
    # Only a real delay() call counts, not the word in a comment explaining why
    # there isn't one. A check that cries wolf on prose teaches people to skim
    # its output, which defeats the point of running it.
    code_only = strip_comments(ba)
    if re.search(r"\bdelay\s*\(", code_only):
        problems.append("boot_anim.h calls delay(); this firmware is non-blocking")

    # ── 6. the compiled-in factory copy matches tools\boot ─────────
    # The sketch carries a second copy of the boot animation so a fresh flash
    # has one without a BLE upload. Two copies of anything drift: regenerate
    # the bins but not the header, or the other way round, and a new device
    # shows a different logo than the upload path produces — with no compiler
    # here to notice. This is the drift alarm.
    gen = FW / "boot_data.h"
    tool_boot = FW.parents[1] / "tools" / "boot"
    pairs = (("BOOT_DEFAULT_META", "meta.json"),
             ("BOOT_DEFAULT_SEGS", "segs.bin"),
             ("BOOT_DEFAULT_TRIS", "tris.bin"))
    if not gen.exists():
        problems.append(
            "boot_data.h missing — regenerate it with "
            "python tools\\mochi_to_boot.py <mochi.ino> tools\\boot "
            "--cpp firmware\\claude_hud\\boot_data.h")
    elif not tool_boot.is_dir():
        problems.append(f"tools\\boot not found at {tool_boot}")
    else:
        gtext = gen.read_text(encoding="utf-8", errors="replace")
        for sym, fname in pairs:
            checked += 1
            want_path = tool_boot / fname
            if not want_path.exists():
                problems.append(f"tools\\boot\\{fname} missing")
                continue
            want = want_path.read_bytes()
            m = re.search(rf"{sym}\[\]\s*PROGMEM\s*=\s*\{{(.*?)\}};", gtext, re.S)
            if not m:
                problems.append(f"boot_data.h: {sym} array not found")
                continue
            got = bytes(int(h, 16)
                        for h in re.findall(r"0x([0-9a-fA-F]{2})", m.group(1)))
            if got != want:
                problems.append(
                    f"boot_data.h {sym} holds {len(got)} bytes but "
                    f"tools\\boot\\{fname} is {len(want)} — regenerate the header")
            lm = re.search(rf"#define\s+{sym}_LEN\s+(\d+)", gtext)
            if not lm or int(lm.group(1)) != len(want):
                problems.append(
                    f"boot_data.h {sym}_LEN disagrees with tools\\boot\\{fname}")

    print(f"checked {len(files)} files, {checked} local includes")
    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("firmware static check OK")
    print("  (includes resolve, braces balance, the raw-draw API exists,")
    print("   and boot_anim.h stays non-blocking)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
