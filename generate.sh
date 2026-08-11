#!/usr/bin/env bash
# Regenerate the Alpha firmware's gen/ keymap tables (append-only; see
# generate_alpha.py). Usage: ./generate.sh /path/to/alpha-firmware
# Runs in Docker so the XKB data version is pinned (ubuntu:24.04 /
# xkb-data 2.41); clang-format is applied on the host afterwards.
set -euo pipefail
FW="$(cd "$1" && pwd)"
REPO="$(cd "$(dirname "$0")" && pwd)"
docker build -q -t alpha-xkb-gen "$REPO/tools" >/dev/null
docker run --rm -v "$REPO":/repo -v "$FW":/firmware alpha-xkb-gen \
    python3 /repo/generate_alpha.py --repo /repo --firmware /firmware
CF=$(command -v clang-format || echo /Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang-format)
find "$FW/main/modules/keyboard/gen" -name '*.c' -o -name '*.h' | xargs "$CF" -i --style=file
echo "clang-format applied"
