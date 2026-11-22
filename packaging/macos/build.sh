#!/bin/bash
# Builds dist/TetherInvert.app and dist/TetherInvert-<version>-<arch>.dmg. The same script runs on a Mac
# and on the GitHub runner (.github/workflows/app.yml).
#
#   packaging/macos/build.sh
#
# Environment (all optional):
#   PYTHON                 interpreter to build with (default python3); the app carries its own copy
#   TETHERINVERT_ARCH      arm64 (default) or x86_64; must match the interpreter and the machine
#   TETHERINVERT_CODESIGN  a "Developer ID Application: ..." identity in the keychain; unset = ad hoc
#   NOTARY_PROFILE         with TETHERINVERT_CODESIGN: notarize the DMG with these notarytool keychain
#                          credentials (xcrun notarytool store-credentials) and staple the ticket to it
#   NOTARY_API_KEY, NOTARY_API_KEY_ID, NOTARY_API_ISSUER
#                          the same without a keychain profile (the GitHub runner): the path of an
#                          App Store Connect API key (.p8), its key ID and the issuer ID
#
# The build uses its own virtual environment (build/macos-venv), never the one you develop in.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HERE="$ROOT/packaging/macos"
if [ -z "${TETHERINVERT_CODESIGN+set}" ] && [ -f "$HERE/signing.env" ]; then
    while IFS='=' read -r key value; do  # read, not sourced: the identity has spaces and brackets
        [ -n "$key" ] && export "$key=$value"
    done < "$HERE/signing.env"
fi
PYTHON=${PYTHON:-python3}
export TETHERINVERT_ARCH=${TETHERINVERT_ARCH:-arm64}
VENV="$ROOT/build/macos-venv"
APP="$ROOT/dist/TetherInvert.app"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/tetherinvert/__init__.py")
DMG="$ROOT/dist/TetherInvert-$VERSION-macos-$TETHERINVERT_ARCH.dmg"

if [ "$(uname -m)" != "$TETHERINVERT_ARCH" ]; then
    echo "This Mac is $(uname -m); building for $TETHERINVERT_ARCH needs a $TETHERINVERT_ARCH machine (the wheels are per architecture)." >&2
    exit 1
fi

echo "== TetherInvert $VERSION for macOS $TETHERINVERT_ARCH, with $("$PYTHON" --version)"
"$PYTHON" -m venv --clear "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
# The build in words, for Help › About (tetherinvert/about.py): written into the package before it is
# installed into the build's environment, removed again when the script ends.
BUILD_FILE="$ROOT/tetherinvert/_build.py"
trap 'rm -f "$BUILD_FILE"' EXIT
COMMIT=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
git -C "$ROOT" diff --quiet HEAD 2>/dev/null || COMMIT="$COMMIT with local changes"
RUN=${GITHUB_RUN_NUMBER:+, GitHub run $GITHUB_RUN_NUMBER}
echo "BUILD = \"app, commit $COMMIT, built $(date -u '+%Y-%m-%d %H:%M') UTC${RUN:-, on a Mac}\"" > "$BUILD_FILE"
"$VENV/bin/python" -m pip install --quiet "$ROOT" "pyinstaller>=6.16"

echo "== freezing"
rm -rf "$APP" "$ROOT/dist/tetherinvert" "$DMG"
"$VENV/bin/pyinstaller" --noconfirm --clean --log-level WARN \
    --distpath "$ROOT/dist" --workpath "$ROOT/build/pyinstaller" "$ROOT/packaging/tetherinvert.spec"
rm -rf "$ROOT/dist/tetherinvert"  # the unbundled folder COLLECT leaves next to the .app

# PyInstaller signed every file: ad hoc, or with the identity, a timestamp, the hardened runtime
# and the entitlements, which is what notarization asks for.
codesign --verify --deep --strict "$APP"
if [ -n "${TETHERINVERT_CODESIGN:-}" ]; then
    codesign -dv "$APP" 2>&1 | grep -q "flags=.*runtime" || { echo "signed without the hardened runtime" >&2; exit 1; }
    echo "ok    signed by $TETHERINVERT_CODESIGN, hardened runtime"
fi

echo "== self-test of the frozen app"
"$APP/Contents/MacOS/tetherinvert" --selftest
SMOKE=$(mktemp -d)
TETHERINVERT_HOME="$SMOKE" "$APP/Contents/MacOS/tetherinvert" --sim doctor > "$SMOKE/doctor.txt" || { cat "$SMOKE/doctor.txt"; exit 1; }
grep -q "RESULT: this camera's files decode" "$SMOKE/doctor.txt" || { cat "$SMOKE/doctor.txt"; exit 1; }
echo "ok    simulated rig: a test shot is captured and decodes"
rm -rf "$SMOKE"

echo "== disk image"
STAGE=$(mktemp -d)
ditto "$APP" "$STAGE/TetherInvert.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "TetherInvert $VERSION" -srcfolder "$STAGE" -fs HFS+ -format UDZO "$DMG"
rm -rf "$STAGE"

if [ -n "${TETHERINVERT_CODESIGN:-}" ]; then
    codesign --timestamp --sign "$TETHERINVERT_CODESIGN" "$DMG"
    if [ -n "${NOTARY_PROFILE:-}" ]; then
        NOTARY=(--keychain-profile "$NOTARY_PROFILE")
    elif [ -n "${NOTARY_API_KEY:-}" ]; then
        NOTARY=(--key "$NOTARY_API_KEY" --key-id "$NOTARY_API_KEY_ID" --issuer "$NOTARY_API_ISSUER")
    fi
    if [ -n "${NOTARY+set}" ]; then
        echo "== notarizing (usually a few minutes)"
        # notarytool exits 0 even when Apple rejects the file, so the verdict is read from its answer.
        RESULT=$(xcrun notarytool submit "$DMG" "${NOTARY[@]}" --wait --output-format json)
        STATUS=$(plutil -extract status raw - <<< "$RESULT")
        if [ "$STATUS" != "Accepted" ]; then
            echo "notarization: $STATUS. Apple's log:" >&2
            xcrun notarytool log "$(plutil -extract id raw - <<< "$RESULT")" "${NOTARY[@]}" >&2
            exit 1
        fi
        xcrun stapler staple "$DMG"
        spctl --assess --type open --context context:primary-signature --verbose "$DMG"
        echo "ok    notarized and stapled: opens on any Mac without a warning"
    fi
fi

echo "== done"
du -sh "$APP" "$DMG"
