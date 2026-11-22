#!/bin/bash
# Builds dist/TetherInvert/ and dist/TetherInvert-<version>-linux-<arch>.tar.gz, for the machine's own
# architecture (x86_64 or aarch64: the wheels are per architecture). The same script runs on a Linux
# machine and on the GitHub runners (.github/workflows/app.yml).
#
#   packaging/linux/build.sh
#
# Environment (optional):
#   PYTHON   interpreter to build with (default python3); the app carries its own copy
#
# The frozen app needs the glibc of the machine it was built on or newer, so build on the oldest
# system it should run on. PySide6's wheels set the floor: glibc 2.34 on x86_64, 2.39 on aarch64.
# Qt's X11 libraries (libxcb-cursor0 and friends) are copied from the build machine when installed
# there; see the workflow for the list.
#
# The build uses its own virtual environment (build/linux-venv), never the one you develop in.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HERE="$ROOT/packaging/linux"
PYTHON=${PYTHON:-python3}
ARCH=$(uname -m)
VENV="$ROOT/build/linux-venv"
APP="$ROOT/dist/TetherInvert"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/tetherinvert/__init__.py")
TARBALL="$ROOT/dist/TetherInvert-$VERSION-linux-$ARCH.tar.gz"

echo "== TetherInvert $VERSION for Linux $ARCH, with $("$PYTHON" --version), glibc $(ldd --version | head -1 | grep -o '[0-9.]*$')"
"$PYTHON" -m venv --clear "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
# The build in words, for Help › About (tetherinvert/about.py): written into the package before it is
# installed into the build's environment, removed again when the script ends.
BUILD_FILE="$ROOT/tetherinvert/_build.py"
trap 'rm -f "$BUILD_FILE"' EXIT
COMMIT=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
git -C "$ROOT" diff --quiet HEAD 2>/dev/null || COMMIT="$COMMIT with local changes"
RUN=${GITHUB_RUN_NUMBER:+, GitHub run $GITHUB_RUN_NUMBER}
echo "BUILD = \"app, commit $COMMIT, built $(date -u '+%Y-%m-%d %H:%M') UTC${RUN:-, on Linux}\"" > "$BUILD_FILE"
"$VENV/bin/python" -m pip install --quiet "$ROOT" "pyinstaller>=6.16"

echo "== freezing"
rm -rf "$APP" "$TARBALL" "$ROOT/build/linux-dist"
# Frozen outside dist/: renamed there, tetherinvert -> TetherInvert is a no-op on a case-insensitive disk.
"$VENV/bin/pyinstaller" --noconfirm --clean --log-level WARN \
    --distpath "$ROOT/build/linux-dist" --workpath "$ROOT/build/pyinstaller" "$ROOT/packaging/tetherinvert.spec"
mkdir -p "$ROOT/dist"
mv "$ROOT/build/linux-dist/tetherinvert" "$APP"
cp "$HERE/install.sh" "$APP/install.sh"
cp "$ROOT/docs/logo.png" "$APP/tetherinvert.png"

echo "== self-test of the frozen app"
"$APP/tetherinvert" --selftest
if command -v xvfb-run > /dev/null; then  # the window on X11 for real, not only offscreen
    QT_QPA_PLATFORM=xcb xvfb-run -a "$APP/tetherinvert" --selftest > /dev/null
    echo "ok    Qt's X11 platform plugin loads (under Xvfb)"
fi
SMOKE=$(mktemp -d)
TETHERINVERT_HOME="$SMOKE" "$APP/tetherinvert" --sim doctor > "$SMOKE/doctor.txt" || { cat "$SMOKE/doctor.txt"; exit 1; }
grep -q "RESULT: this camera's files decode" "$SMOKE/doctor.txt" || { cat "$SMOKE/doctor.txt"; exit 1; }
echo "ok    simulated rig: a test shot is captured and decodes"
rm -rf "$SMOKE"

echo "== tarball"
tar -C "$ROOT/dist" -czf "$TARBALL" TetherInvert

echo "== done"
du -sh "$APP" "$TARBALL"
