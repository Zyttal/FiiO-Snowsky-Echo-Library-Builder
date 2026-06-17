#!/usr/bin/env bash
# Build a Linux AppImage of echo-library-builder.
# Run from the project root with the pyenv venv active.
#
# Requires:
#   - pyinstaller in the active venv (added at first use below)
#   - appimagetool on PATH (https://github.com/AppImage/AppImageKit)
#   - curl, tar
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PKG="$ROOT/packaging"
FFMPEG_DIR="$PKG/ffmpeg/linux"
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
    curl -sSL "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" \
        -o "$tmp/ffmpeg.tar.xz"
    tar -xJf "$tmp/ffmpeg.tar.xz" -C "$tmp"
    cp "$tmp"/ffmpeg-*-amd64-static/ffmpeg "$FFMPEG_DIR/"
    chmod +x "$FFMPEG_DIR/ffmpeg"
    rm -rf "$tmp"
fi

echo ">>> Running PyInstaller"
pyenv exec pyinstaller --clean --noconfirm packaging/pyinstaller.spec

EXE="$DIST/echo-library-builder"
if [[ ! -x "$EXE" ]]; then
    echo "PyInstaller did not produce $EXE" >&2
    exit 1
fi

echo ">>> Assembling AppDir"
APPDIR="$BUILD/echo-library-builder.AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
cp "$EXE" "$APPDIR/usr/bin/"
cat > "$APPDIR/echo-library-builder.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=echo-library-builder
Exec=echo-library-builder
Icon=echo-library-builder
Categories=AudioVideo;
Terminal=false
DESKTOP
# Placeholder icon — replace with a real one if/when we draw one.
touch "$APPDIR/echo-library-builder.png"
cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/echo-library-builder" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

echo ">>> Running appimagetool"
if ! command -v appimagetool >/dev/null; then
    echo "appimagetool not on PATH. Install from https://github.com/AppImage/AppImageKit" >&2
    exit 1
fi
ARCH=x86_64 appimagetool "$APPDIR" "$DIST/echo-library-builder-x86_64.AppImage"

echo ">>> Done: $DIST/echo-library-builder-x86_64.AppImage"
