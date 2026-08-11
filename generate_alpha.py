#!/usr/bin/env python3
"""Generate the Alpha firmware's gen/ keymap tables from XKB data.

Runs on Linux (use ./generate.sh, which wraps this in Docker with a pinned
xkb-data) against a checkout of alpha-firmware. The generator is APPEND-ONLY
by design:

  - layouts/<code>.c files that already exist are FROZEN output and are never
    rewritten (the shipped keysym indices inside them must not move).
  - keysyms.c keeps every existing entry at its existing index; keysyms that
    new layouts need are appended after the last entry.
  - unicode_capitalizations.c / unicode_downgrades.c grow additively.
  - keymaps.{c,h} are regenerated wholesale from alpha/config.json (they are
    pure registries; the config order IS the layout_index_t enum order and is
    append-only because the @postbox/types ALPHA_KEYMAP_BASE_LAYOUTS mirror
    and the runtime keymap registry (ADR-0015) index space depend on it).

Every printable a layout emits must be renderable on the device: its
codepoint must be covered by the Alpha fonts (alpha/font_codepoints.txt,
extracted from font/FSTNLCDALRegular-15.bdf) or have an entry in
unicode_downgrades. Anything else is dropped to KM_NONE - the same rule the
historical generator applied (e.g. the arrows on pt's AltGr layer, or e~ on
fi's). A layout whose LANGUAGE-ESSENTIAL letters are unrenderable must not be
added at all (that is a config-review decision, like the az removal in 1.19.1).

Output C is piped through clang-format (the firmware's .clang-format); all
gen/ files are format-stable, so generated files land byte-consistent with
the shipped style.
"""
import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata

# ---------------------------------------------------------------- xkb access

lib = None


class RuleNames(ctypes.Structure):
    _fields_ = [
        ("rules", ctypes.c_char_p),
        ("model", ctypes.c_char_p),
        ("layout", ctypes.c_char_p),
        ("variant", ctypes.c_char_p),
        ("options", ctypes.c_char_p),
    ]


def xkb_init():
    global lib
    lib = ctypes.CDLL("libxkbcommon.so.0")
    lib.xkb_context_new.restype = ctypes.c_void_p
    lib.xkb_keymap_new_from_names.restype = ctypes.c_void_p
    lib.xkb_keymap_new_from_names.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    lib.xkb_keymap_key_get_syms_by_level.restype = ctypes.c_int
    lib.xkb_keymap_key_get_syms_by_level.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint32)),
    ]
    lib.xkb_keysym_to_utf32.restype = ctypes.c_uint32
    lib.xkb_keysym_to_utf32.argtypes = [ctypes.c_uint32]
    lib.xkb_keysym_get_name.restype = ctypes.c_int
    lib.xkb_keysym_get_name.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t]


# Alpha physical keys that take per-layout printables, in keycode_t order,
# with the X/evdev keycode each maps to on a pc104 XKB keymap.
XKB_KEYS = [
    ("KEYCODE_GRAVE", 49), ("KEYCODE_1", 10), ("KEYCODE_2", 11), ("KEYCODE_3", 12),
    ("KEYCODE_4", 13), ("KEYCODE_5", 14), ("KEYCODE_6", 15), ("KEYCODE_7", 16),
    ("KEYCODE_8", 17), ("KEYCODE_9", 18), ("KEYCODE_0", 19), ("KEYCODE_MINUS", 20),
    ("KEYCODE_EQUAL", 21),
    ("KEYCODE_Q", 24), ("KEYCODE_W", 25), ("KEYCODE_E", 26), ("KEYCODE_R", 27),
    ("KEYCODE_T", 28), ("KEYCODE_Y", 29), ("KEYCODE_U", 30), ("KEYCODE_I", 31),
    ("KEYCODE_O", 32), ("KEYCODE_P", 33), ("KEYCODE_BRACE_LEFT", 34),
    ("KEYCODE_BRACE_RIGHT", 35), ("KEYCODE_BACKSLASH", 51),
    ("KEYCODE_A", 38), ("KEYCODE_S", 39), ("KEYCODE_D", 40), ("KEYCODE_F", 41),
    ("KEYCODE_G", 42), ("KEYCODE_H", 43), ("KEYCODE_J", 44), ("KEYCODE_K", 45),
    ("KEYCODE_L", 46), ("KEYCODE_SEMICOLON", 47), ("KEYCODE_QUOTE", 48),
    ("KEYCODE_Z", 52), ("KEYCODE_X", 53), ("KEYCODE_C", 54), ("KEYCODE_V", 55),
    ("KEYCODE_B", 56), ("KEYCODE_N", 57), ("KEYCODE_M", 58), ("KEYCODE_COMMA", 59),
    ("KEYCODE_PERIOD", 60), ("KEYCODE_SLASH", 61),
]

# The full keycode_t emission order (matches keymap_types.h).
KEYCODE_ORDER = [
    "KEYCODE_GRAVE", "KEYCODE_1", "KEYCODE_2", "KEYCODE_3", "KEYCODE_4",
    "KEYCODE_5", "KEYCODE_6", "KEYCODE_7", "KEYCODE_8", "KEYCODE_9",
    "KEYCODE_0", "KEYCODE_MINUS", "KEYCODE_EQUAL", "KEYCODE_BACKSPACE",
    "KEYCODE_TAB", "KEYCODE_Q", "KEYCODE_W", "KEYCODE_E", "KEYCODE_R",
    "KEYCODE_T", "KEYCODE_Y", "KEYCODE_U", "KEYCODE_I", "KEYCODE_O",
    "KEYCODE_P", "KEYCODE_BRACE_LEFT", "KEYCODE_BRACE_RIGHT",
    "KEYCODE_BACKSLASH", "KEYCODE_CAPS_LOCK", "KEYCODE_A", "KEYCODE_S",
    "KEYCODE_D", "KEYCODE_F", "KEYCODE_G", "KEYCODE_H", "KEYCODE_J",
    "KEYCODE_K", "KEYCODE_L", "KEYCODE_SEMICOLON", "KEYCODE_QUOTE",
    "KEYCODE_ENTER", "KEYCODE_SHIFT_LEFT", "KEYCODE_Z", "KEYCODE_X",
    "KEYCODE_C", "KEYCODE_V", "KEYCODE_B", "KEYCODE_N", "KEYCODE_M",
    "KEYCODE_COMMA", "KEYCODE_PERIOD", "KEYCODE_SLASH", "KEYCODE_SHIFT_RIGHT",
    "KEYCODE_NEW_LEFT", "KEYCODE_PAGE_UP", "KEYCODE_PAGE_DOWN",
    "KEYCODE_SPACE", "KEYCODE_ALTGR", "KEYCODE_SEND", "KEYCODE_SPECIAL",
    "KEYCODE_NEW_RIGHT",
]

# Command keys carry the same keysym in every layer of every layout.
COMMAND_TEMPLATE = {
    "KEYCODE_BACKSPACE": ("C", "COMMAND_KEY_BACKSPACE"),
    "KEYCODE_CAPS_LOCK": ("C", "COMMAND_KEY_CAPSLOCK"),
    "KEYCODE_SHIFT_LEFT": ("C", "COMMAND_KEY_SHIFT"),
    "KEYCODE_SHIFT_RIGHT": ("C", "COMMAND_KEY_SHIFT"),
    "KEYCODE_NEW_LEFT": ("C", "COMMAND_KEY_NEW"),
    "KEYCODE_NEW_RIGHT": ("C", "COMMAND_KEY_NEW"),
    "KEYCODE_PAGE_UP": ("C", "COMMAND_KEY_PAGEUP"),
    "KEYCODE_PAGE_DOWN": ("C", "COMMAND_KEY_PAGEDOWN"),
    "KEYCODE_ALTGR": ("C", "COMMAND_KEY_ALTGR"),
    "KEYCODE_SEND": ("C", "COMMAND_KEY_SEND"),
    "KEYCODE_SPECIAL": ("C", "COMMAND_KEY_SPECIAL"),
    "KEYCODE_TAB": ("P", 0x9),
    "KEYCODE_ENTER": ("P", 0xA),
}

DEAD_MAP = {
    "dead_acute": "DEAD_KEY_ACUTE", "dead_grave": "DEAD_KEY_GRAVE",
    "dead_circumflex": "DEAD_KEY_CIRCUMFLEX", "dead_diaeresis": "DEAD_KEY_DIAERESIS",
    "dead_tilde": "DEAD_KEY_TILDE", "dead_caron": "DEAD_KEY_CARON",
    "dead_abovedot": "DEAD_KEY_ABOVEDOT", "dead_doubleacute": "DEAD_KEY_DOUBLEACUTE",
    "dead_cedilla": "DEAD_KEY_CEDILLA", "dead_ogonek": "DEAD_KEY_OGONEK",
    "dead_breve": "DEAD_KEY_BREVE", "dead_abovering": "DEAD_KEY_ABOVERING",
    "dead_macron": "DEAD_KEY_MACRON", "dead_hook": "DEAD_KEY_HOOK",
    "dead_horn": "DEAD_KEY_HORN", "dead_belowdot": "DEAD_KEY_BELOWDOT",
    "dead_stroke": "DEAD_KEY_STROKE", "dead_greek": "DEAD_KEY_GREEK",
    "dead_doublegrave": "DEAD_KEY_DOUBLEGRAVE",
    "dead_invertedbreve": "DEAD_KEY_INVERTEDBREVE",
    "dead_belowcomma": "DEAD_KEY_BELOWCOMMA", "dead_currency": "DEAD_KEY_CURRENCY",
    "dead_abovereversedcomma": "DEAD_KEY_ABOVEREVERSEDCOMMA",
    "dead_abovecomma": "DEAD_KEY_ABOVECOMMA", "dead_iota": "DEAD_KEY_IOTA",
    "Multi_key": "DEAD_KEY_MULTIKEY",
}

GENERATED_BANNER = "// This file is generated by github.com/astrohaus/alpha-keyboard-layouts-from-linux"


def keysym_name(ks):
    buf = ctypes.create_string_buffer(64)
    lib.xkb_keysym_get_name(ks, buf, 64)
    return buf.value.decode()


def extract_layout(model, layout, variant):
    """-> {alpha_key: [entry or None per level 0..3]} where entry is
    ("P", cp) | ("D", DEAD_KEY_*) | ("C", COMMAND_KEY_ALTGR_LATCH)."""
    ctx = lib.xkb_context_new(0)
    names = RuleNames(b"evdev", model.encode(), layout.encode(), variant.encode(), b"")
    km = lib.xkb_keymap_new_from_names(ctx, ctypes.byref(names), 0)
    if not km:
        raise RuntimeError(f"xkb compile failed for {model}/{layout}/{variant}")
    out = {}
    for key, xkc in XKB_KEYS:
        levels = []
        for lvl in range(4):
            syms = ctypes.POINTER(ctypes.c_uint32)()
            n = lib.xkb_keymap_key_get_syms_by_level(km, xkc, 0, lvl, ctypes.byref(syms))
            if n < 1:
                levels.append(None)
                continue
            nm = keysym_name(syms[0])
            if nm in DEAD_MAP:
                levels.append(("D", DEAD_MAP[nm]))
            elif nm == "ISO_Level3_Latch":
                levels.append(("C", "COMMAND_KEY_ALTGR_LATCH"))
            else:
                cp = lib.xkb_keysym_to_utf32(syms[0])
                levels.append(("P", cp) if cp else None)
        out[key] = levels
    return out


# ------------------------------------------------------------ gen/ parsing

def parse_keysyms(path):
    """-> ordered list of ("P", cp) | ("C", name) | ("D", name) | ("N",)."""
    arr = []
    for line in open(path):
        m = re.search(r"KM_PRINTABLE\(0x([0-9A-Fa-f]+)\)", line)
        if m:
            arr.append(("P", int(m.group(1), 16)))
            continue
        m = re.search(r"KM_COMMAND\((\w+)\)", line)
        if m:
            arr.append(("C", m.group(1)))
            continue
        m = re.search(r"KM_DEAD_KEY\((\w+)\)", line)
        if m:
            arr.append(("D", m.group(1)))
            continue
        if re.search(r"KM_NONE\(\)", line):
            arr.append(("N",))
    return arr


def parse_pairs(path, array_name):
    """Parse a `const uint32_t NAME[n] = { 0x.., // c -> C ... }` array."""
    src = open(path, encoding="utf-8").read()
    m = re.search(re.escape(array_name) + r"\[\d+\] = \{(.*?)\};", src, re.S)
    vals = re.findall(r"0x([0-9A-Fa-f]+),", m.group(1))
    return [int(v, 16) for v in vals]


def parse_downgrades(path):
    src = open(path, encoding="utf-8").read()
    return [
        (int(a, 16), int(b, 16))
        for a, b in re.findall(r"\{\.source = 0x([0-9A-Fa-f]+), \.downgraded = 0x([0-9A-Fa-f]+)\}", src)
    ]


# ------------------------------------------------------------ C emission

def char_comment(entry):
    if entry[0] == "N":
        return "NONE"
    if entry[0] in ("C", "D"):
        return entry[1]
    cp = entry[1]
    if cp == 0x9:
        return "'\\t'"
    if cp == 0xA:
        return "'\\n'"
    ch = chr(cp)
    if ch == "'":
        return '"\'"'
    if ch == "\\":
        return "'\\\\'"
    if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp", "Mn"):
        return f"U+{cp:04X}"
    return f"'{ch}'"


def clang_format(paths, fw_root):
    subprocess.run(["clang-format", "-i", "--style=file"] + paths, cwd=fw_root, check=True)


def emit_layout_c(code, name, table, keysym_index_of, out_path):
    lines = [GENERATED_BANNER, "", '#include "gen/keymap_types.h"', '#include "gen/keysyms.h"', ""]
    lines.append(f"const layout_t layout_{code} = {{")
    lines.append(f'    .id = "{code}",')
    lines.append(f'    .name = "{name}",')
    lines.append("    .keylookup =")
    lines.append("        {")
    for li, lname in enumerate(["LAYER_BASE", "LAYER_SHIFT", "LAYER_ALTGR", "LAYER_ALTGR_SHIFT"]):
        lines.append(f"            // {lname}")
        lines.append("            {")
        for key in KEYCODE_ORDER:
            entry = table[li][key]
            if entry[0] == "N":
                continue  # designated initializers default to 0 (KM_NONE)
            idx = keysym_index_of[entry]
            lines.append(f"                [{key}] = {idx}, // {char_comment(entry)}")
        lines.append("            },")
    lines.append("        },")
    lines.append("};")
    open(out_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def emit_keysyms_c(path, new_entries):
    """Append new entries to keysym_array, leaving every existing line
    byte-identical (the shipped indices and their comments are frozen)."""
    src = open(path, encoding="utf-8").read()
    add = ""
    for e in new_entries:
        if e[0] == "P":
            add += f"    KM_PRINTABLE(0x{e[1]:X}), // {char_comment(e)}\n"
        elif e[0] == "C":
            add += f"    KM_COMMAND({e[1]}), // {e[1]}\n"
        else:
            add += f"    KM_DEAD_KEY({e[1]}), // {e[1]}\n"
    m = re.search(r"static const keysym_t keysym_array\[\] = \{.*?(\};)", src, re.S)
    src = src[: m.start(1)] + add + src[m.start(1):]
    open(path, "w", encoding="utf-8").write(src)


def emit_keymaps_h(codes, out_path):
    lines = ["#pragma once", "", GENERATED_BANNER, "", '#include "keymap_types.h"', "",
             "#include <stdbool.h>", "#include <stdint.h>", "",
             "#ifdef __cplusplus", 'extern "C" {', "#endif", "",
             "typedef enum layout_index {"]
    for c in codes:
        lines.append(f"    LAYOUT_{c.upper()},")
    lines += ["", "    LAYOUT_COUNT,", "} layout_index_t;", "",
              "const layout_t *get_layout_by_id(const char *name);",
              "const layout_t *get_layout_by_index(uint32_t index);",
              "bool get_layout_index(const layout_t *layout, uint32_t *index);", "",
              "#ifdef __cplusplus", "}", "#endif"]
    open(out_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def emit_keymaps_c(codes, out_path):
    lines = [GENERATED_BANNER, "", '#include "gen/keymaps.h"', '#include "gen/keymap_types.h"', "",
             "#include <stdbool.h>", "#include <stdint.h>", "#include <string.h>", ""]
    for c in codes:
        lines.append(f"extern const layout_t layout_{c};")
    lines += ["", "static const layout_t *layouts[] = {"]
    for c in codes:
        lines.append(f"    &layout_{c},")
    lines += ["};", "",
              "const layout_t *get_layout_by_id(const char *name) {",
              "    for (int i = 0; i < LAYOUT_COUNT; i++) {",
              "        if (strcmp(name, layouts[i]->id) == 0) {",
              "            return layouts[i];",
              "        }",
              "    }",
              "    return NULL;",
              "}", "",
              "const layout_t *get_layout_by_index(uint32_t index) {",
              "    if (index >= LAYOUT_COUNT) {",
              "        return NULL;",
              "    }",
              "    return layouts[index];",
              "}", "",
              "bool get_layout_index(const layout_t *layout, uint32_t *index) {",
              "    for (uint32_t i = 0; i < LAYOUT_COUNT; i++) {",
              "        if (layouts[i] == layout) {",
              "            *index = i;",
              "            return true;",
              "        }",
              "    }",
              "    return false;",
              "}"]
    open(out_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def append_case_pairs(path, new_pairs):
    """Append (lower, upper) pairs to both arrays in unicode_capitalizations.c."""
    src = open(path, encoding="utf-8").read()
    lo = parse_pairs(path, "const uint32_t UNICODE_LOWERCASE")
    n = len(lo) + len(new_pairs)
    for lower, upper in new_pairs:
        lc, uc = chr(lower), chr(upper)
        m = re.search(r"(const uint32_t UNICODE_LOWERCASE)\[\d+\]( = \{.*?)(\};)", src, re.S)
        src = src[: m.end(2)] + f"    0x{lower:X}, // {lc} -> {uc}\n" + src[m.end(2):]
        m = re.search(r"(const uint32_t UNICODE_UPPERCASE)\[\d+\]( = \{.*?)(\};)", src, re.S)
        src = src[: m.end(2)] + f"    0x{upper:X}, // {uc} -> {lc}\n" + src[m.end(2):]
    src = re.sub(r"(const uint32_t UNICODE_(?:LOWER|UPPER)CASE)\[\d+\]", rf"\1[{n}]", src)
    src = re.sub(r"(for \(uint32_t i = 0; i < )\d+(;)", rf"\g<1>{n}\2", src)
    open(path, "w", encoding="utf-8").write(src)


def append_downgrades(path, new_entries):
    src = open(path, encoding="utf-8").read()
    existing = parse_downgrades(path)
    n = len(existing) + len(new_entries)
    m = re.search(r"(const downgrade_t UNICODE_DOWNGRADES)\[\d+\]( = \{.*?)(\};)", src, re.S)
    add = ""
    for source, downgraded in new_entries:
        add += f"    {{.source = 0x{source:X}, .downgraded = 0x{downgraded:X}}}, // {chr(source)} -> {chr(downgraded)}\n"
    src = src[: m.end(2)] + add + src[m.end(2):]
    src = re.sub(r"(const downgrade_t UNICODE_DOWNGRADES)\[\d+\]", rf"\1[{n}]", src)
    src = re.sub(r"(for \(uint32_t i = 0; i < )\d+(;)", rf"\g<1>{n}\2", src)
    open(path, "w", encoding="utf-8").write(src)


# ------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--firmware", required=True, help="alpha-firmware checkout root")
    ap.add_argument("--repo", default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()

    xkb_init()
    gen = os.path.join(args.firmware, "main/modules/keyboard/gen")
    cfg = json.load(open(os.path.join(args.repo, "alpha/config.json")))
    font = set(int(l, 16) for l in open(os.path.join(args.repo, "alpha/font_codepoints.txt")))

    keysyms = parse_keysyms(os.path.join(gen, "keysyms.c"))
    index_of = {e: i for i, e in enumerate(keysyms)}  # first index wins
    downgrades = dict(parse_downgrades(os.path.join(gen, "unicode_downgrades.c")))

    new_downgrades = [(int(d["source"], 16), int(d["downgraded"], 16)) for d in cfg.get("downgrades", [])]
    for s, d in new_downgrades:
        if s not in downgrades:
            if d not in font:
                sys.exit(f"config downgrade target U+{d:04X} not in font")
            downgrades[s] = d
    renderable = font | set(downgrades) | {0x9, 0xA, 0x20}

    codes = [l["code"] for l in cfg["layouts"]]
    changed = []
    new_keysyms = []

    def resolve(entry):
        """Map an extracted entry to a keysym-table entry, appending if new."""
        if entry is None:
            return ("N",)
        if entry[0] == "P":
            if entry[1] not in renderable:
                return ("N",)
            e = ("P", entry[1])
        elif entry[0] == "D":
            e = ("D", entry[1])
            if e not in index_of:
                sys.exit(f"dead key {entry[1]} missing from keysym table")
        else:
            e = ("C", entry[1])
        if e not in index_of:
            index_of[e] = len(keysyms)
            keysyms.append(e)
            new_keysyms.append(e)
        return e

    for lay in cfg["layouts"]:
        code = lay["code"]
        path = os.path.join(gen, "layouts", f"{code}.c")
        if os.path.exists(path):
            continue  # frozen
        raw = extract_layout(lay["model"], lay["layout"], lay["variant"])
        # whole-layer backfill: no AltGr data at all -> AltGr mirrors base/shift
        lvl_has = [any(raw[k][lvl] is not None for k, _ in XKB_KEYS) for lvl in range(4)]
        table = []
        for lvl in range(4):
            src_lvl = lvl
            if lvl >= 2 and not lvl_has[2] and not lvl_has[3]:
                src_lvl = lvl - 2
            layer = {}
            for key in KEYCODE_ORDER:
                if key in COMMAND_TEMPLATE:
                    layer[key] = resolve(COMMAND_TEMPLATE[key])
                elif key == "KEYCODE_SPACE":
                    e = resolve(raw.get(key, [None] * 4)[src_lvl]) if key in raw else ("N",)
                    layer[key] = e if e != ("N",) else resolve(("P", 0x20))
                elif key in raw:
                    layer[key] = resolve(raw[key][src_lvl])
                else:
                    layer[key] = ("N",)
            table.append(layer)
        # space is not in XKB_KEYS: default it
        emit_layout_c(code, lay["name"], table, index_of, path)
        changed.append(path)
        print(f"generated layouts/{code}.c")

    # keysyms.c (append-only rewrite)
    if new_keysyms:
        emit_keysyms_c(os.path.join(gen, "keysyms.c"), new_keysyms)
        changed.append(os.path.join(gen, "keysyms.c"))
        print(f"appended {len(new_keysyms)} keysyms: " + ", ".join(char_comment(e) for e in new_keysyms))

    # capitalizations: any new printable with a 1:1 case pair, both sides renderable
    lo = parse_pairs(os.path.join(gen, "unicode_capitalizations.c"), "const uint32_t UNICODE_LOWERCASE")
    have = set(lo)
    pairs = []
    for e in new_keysyms:
        if e[0] != "P":
            continue
        ch = chr(e[1])
        if ch.lower() != ch and len(ch.lower()) == 1:  # uppercase letter
            l, u = ord(ch.lower()), e[1]
        elif ch.upper() != ch and len(ch.upper()) == 1:  # lowercase letter
            l, u = e[1], ord(ch.upper())
        else:
            continue
        if l not in have and l in renderable and u in renderable:
            pairs.append((l, u))
            have.add(l)
    pairs = sorted(set(pairs))
    if pairs:
        append_case_pairs(os.path.join(gen, "unicode_capitalizations.c"), pairs)
        changed.append(os.path.join(gen, "unicode_capitalizations.c"))
        print("appended case pairs: " + ", ".join(f"{chr(l)}/{chr(u)}" for l, u in pairs))

    if new_downgrades:
        fresh = [(s, d) for s, d in new_downgrades if (s, d) not in parse_downgrades(os.path.join(gen, "unicode_downgrades.c"))]
        if fresh:
            append_downgrades(os.path.join(gen, "unicode_downgrades.c"), fresh)
            changed.append(os.path.join(gen, "unicode_downgrades.c"))
            print("appended downgrades: " + ", ".join(f"U+{s:04X}->U+{d:04X}" for s, d in fresh))

    # registries
    emit_keymaps_h(codes, os.path.join(gen, "keymaps.h"))
    emit_keymaps_c(codes, os.path.join(gen, "keymaps.c"))
    changed += [os.path.join(gen, "keymaps.h"), os.path.join(gen, "keymaps.c")]

    if shutil.which("clang-format"):
        clang_format(changed, args.firmware)
    else:
        print("WARNING: clang-format not found; run it on the host before committing")
    print(f"done: {len(codes)} layouts (LAYOUT_COUNT={len(codes)})")


if __name__ == "__main__":
    main()
