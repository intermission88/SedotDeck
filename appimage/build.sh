#!/usr/bin/env bash
# Build DeckPort-<version>-x86_64.AppImage. Runs on x86_64 Linux (CI uses Ubuntu).
# Needs: python3, rsvg-convert (librsvg2-bin), curl.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist}"
mkdir -p "$OUT" && OUT="$(cd "$OUT" && pwd)"  # make it absolute
VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/deckport.py")"
WORK="$(mktemp -d)"
APPDIR="$WORK/DeckPort.AppDir"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$APPDIR/usr/share/deckport" "$APPDIR/usr/share/icons/hicolor/256x256/apps" "$OUT"
cp "$ROOT/deckport.py" "$APPDIR/usr/share/deckport/"
install -m 755 "$ROOT/appimage/AppRun" "$APPDIR/AppRun"
cp "$ROOT/appimage/deckport.desktop" "$APPDIR/deckport.desktop"
rsvg-convert -w 256 -h 256 "$ROOT/assets/icon.svg" -o "$APPDIR/deckport.png"
cp "$APPDIR/deckport.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/deckport.png"
ln -s deckport.png "$APPDIR/.DirIcon"

TOOL="${APPIMAGETOOL:-$WORK/appimagetool}"
if [ ! -x "$TOOL" ]; then
  curl -fsSL -o "$TOOL" \
    https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x "$TOOL"
fi

# CI runners have no FUSE, so let appimagetool unpack itself instead of mounting.
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 "$TOOL" --no-appstream "$APPDIR" "$OUT/DeckPort-$VERSION-x86_64.AppImage"
echo "Built $OUT/DeckPort-$VERSION-x86_64.AppImage"
