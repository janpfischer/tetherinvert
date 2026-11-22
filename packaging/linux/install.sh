#!/bin/sh
# Adds TetherInvert to your desktop's applications and `tetherinvert` to ~/.local/bin, pointing at
# this folder; for you only, no root needed. Run it again after moving the folder.
#
#   ./install.sh             add
#   ./install.sh --remove    take both away again (the folder stays)

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP="$APPS/tetherinvert.desktop"
BIN="$HOME/.local/bin/tetherinvert"

if [ "${1:-}" = "--remove" ]; then
    rm -f "$DESKTOP" "$BIN"
    echo "removed $DESKTOP and $BIN"
    exit 0
fi

mkdir -p "$APPS" "$(dirname "$BIN")"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=TetherInvert
Comment=Tethered film scanning and conversion
Exec="$HERE/tetherinvert"
Icon=$HERE/tetherinvert.png
Categories=Graphics;Photography;
StartupWMClass=tetherinvert
Terminal=false
EOF
ln -sf "$HERE/tetherinvert" "$BIN"
command -v update-desktop-database > /dev/null && update-desktop-database "$APPS" 2> /dev/null || true

echo "TetherInvert is in your applications ($DESKTOP)"
echo "and on the command line as $BIN"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "(~/.local/bin is not on your PATH)" ;; esac
