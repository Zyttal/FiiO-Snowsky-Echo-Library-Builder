#!/usr/bin/env bash
# Build a macOS .app (+ .dmg) of echo-library-builder.
# Run from the project root with the pyenv venv active.
#
# Requires:
#   - pyinstaller in the active venv (added at first use below)
#   - hdiutil (ships with macOS)
#   - curl, unzip
#
# Note: the resulting .app is UNSIGNED. First-time users will need to
# right-click -> Open to bypass Gatekeeper. Document this in the README.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PKG="$ROOT/packaging"
FFMPEG_DIR="$PKG/ffmpeg/macos"
DIST="$ROOT/dist"
BUILD="$ROOT/build"

echo ">>> Cleaning previous build"
rm -rf "$DIST" "$BUILD"

echo ">>> Ensuring pyinstaller is installed"
pyenv exec python -m pip install --quiet pyinstaller

echo ">>> Fetching static ffmpeg"
mkdir -p "$FFMPEG_DIR"
if [[ ! -x "$FFMPEG_DIR/ffmpeg" ]]; then
    tmp="$(mktemp -d)"
    curl -sSL "https://evermeet.cx/ffmpeg/getrelease/zip" -o "$tmp/ffmpeg.zip"
    unzip -q "$tmp/ffmpeg.zip" -d "$tmp"
    cp "$tmp/ffmpeg" "$FFMPEG_DIR/"
    chmod +x "$FFMPEG_DIR/ffmpeg"
    rm -rf "$tmp"
fi

echo ">>> Running PyInstaller"
pyenv exec pyinstaller --clean --noconfirm packaging/pyinstaller.spec

APP="$DIST/echo-library-builder.app"
if [[ ! -d "$APP" ]]; then
    echo "PyInstaller did not produce $APP" >&2
    exit 1
fi

echo ">>> Building .dmg"
DMG="$DIST/echo-library-builder.dmg"
rm -f "$DMG"
hdiutil create -volname "echo-library-builder" -srcfolder "$APP" \
    -ov -format UDZO "$DMG"

echo ">>> Done: $DMG"
echo "    First-time users: right-click -> Open to bypass Gatekeeper."
