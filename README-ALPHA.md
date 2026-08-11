# Alpha keymap generation

`generate_alpha.py` (via `./generate.sh /path/to/alpha-firmware`) regenerates
the Alpha firmware's `main/modules/keyboard/gen/` tables from XKB data. It is
**append-only**: existing `layouts/<code>.c` files are frozen output (their
keysym indices are a shipped contract with web-authored custom keymaps and
the @postbox/types mirror), new keysyms append after the last index, and
`keymaps.{c,h}` regenerate from `alpha/config.json` — whose order IS the
`layout_index_t` enum order.

To add a layout:

1. Append `{code, name, model, layout, variant}` to `alpha/config.json`
   (and mirror the xkb triple into `boardlayouts.json` for the legacy tool).
2. Verify renderability first: every codepoint the layout emits on any of
   its four levels must be in `alpha/font_codepoints.txt` (extracted from
   the firmware's `font/FSTNLCDALRegular-15.bdf` ENCODING lines — all three
   sizes carry identical coverage) or have a `unicode_downgrades` entry.
   Language-essential letters the font lacks disqualify the layout (the
   `az` / schwa precedent); AltGr-only extras drop to `KM_NONE`.
3. Run `./generate.sh <alpha-firmware>` — it generates in Docker
   (ubuntu:24.04, xkb-data 2.41 pinned) and clang-formats with the
   firmware's `.clang-format`.
4. In the firmware: add the new `gen/layouts/<code>.c` to
   `main/CMakeLists.txt`, bump the lockstep pin in
   `test/host/test_custom_keymaps.c`, and update
   `@postbox/types` `ALPHA_KEYMAP_BASE_LAYOUTS` (+ its pins) in the same
   change set — firmware merges FIRST.

Regenerate `alpha/font_codepoints.txt` after any font change:

    python3 -c "print('\n'.join(hex(int(l.split()[1])) for l in open('font/FSTNLCDALRegular-15.bdf', errors='replace') if l.startswith('ENCODING ') and int(l.split()[1])>=0))" > alpha/font_codepoints.txt

The legacy `createCustomKeyBoardLatyouts.py` / `setlanguage.py` xmodmap flow
predates this generator and cannot emit the current C tables; it is kept for
reference only.
