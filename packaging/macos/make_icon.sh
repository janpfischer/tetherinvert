#!/bin/bash
# Makes packaging/macos/tetherinvert.icns (the app icon) and docs/logo.png from docs/logo.svg.
# Run it after changing the logo and commit both results: build.sh only uses the .icns, so
# neither a Mac build nor the GitHub runner needs a renderer.
#
# The SVG uses blend modes and blur, which macOS's own tools do not render; Chrome does.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
CHROME=${CHROME:-"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}
SET=$(mktemp -d)/tetherinvert.iconset
mkdir -p "$SET"

"$CHROME" --headless --disable-gpu --hide-scrollbars --default-background-color=00000000 \
    --window-size=1024,1024 --screenshot="$ROOT/docs/logo.png" "file://$ROOT/docs/logo.svg" 2>/dev/null

for size in 16 32 128 256 512; do
    sips -z $size $size "$ROOT/docs/logo.png" --out "$SET/icon_${size}x${size}.png" >/dev/null
    sips -z $((size * 2)) $((size * 2)) "$ROOT/docs/logo.png" --out "$SET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil --convert icns --output "$ROOT/packaging/macos/tetherinvert.icns" "$SET"
rm -rf "$(dirname "$SET")"
ls -l "$ROOT/docs/logo.png" "$ROOT/packaging/macos/tetherinvert.icns"
