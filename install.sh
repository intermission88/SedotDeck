#!/usr/bin/env bash
# Install SedotDeck for the current user (no sudo, survives SteamOS updates).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/sedotdeck"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"

mkdir -p "$DEST" "$BIN" "$APPS"
cp "$SRC/sedotdeck.py" "$DEST/sedotdeck.py"
chmod +x "$DEST/sedotdeck.py"
ln -sf "$DEST/sedotdeck.py" "$BIN/sedotdeck"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$ICONS"
cp "$SRC/assets/icon.svg" "$ICONS/sedotdeck.svg"

cat > "$APPS/sedotdeck.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SedotDeck
Comment=Update AppImages and PC ports from GitHub Releases
Exec=python3 $DEST/sedotdeck.py serve --window --exit-when-closed
Icon=sedotdeck
Terminal=false
Categories=Utility;
EOF
chmod +x "$APPS/sedotdeck.desktop"

# Desktop shortcut too, if there is a Desktop folder.
if [ -d "$HOME/Desktop" ]; then
  cp "$APPS/sedotdeck.desktop" "$HOME/Desktop/sedotdeck.desktop"
  chmod +x "$HOME/Desktop/sedotdeck.desktop"
fi

command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true

echo "SedotDeck installed."
echo "  Open it from the app menu (Utilities > SedotDeck) or the Desktop icon."
echo "  CLI: $BIN/sedotdeck --help"
