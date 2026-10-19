#!/usr/bin/env bash
# Install DeckPort for the current user (no sudo, survives SteamOS updates).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/deckport"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"

mkdir -p "$DEST" "$BIN" "$APPS"
cp "$SRC/deckport.py" "$DEST/deckport.py"
chmod +x "$DEST/deckport.py"
ln -sf "$DEST/deckport.py" "$BIN/deckport"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$ICONS"
cp "$SRC/assets/icon.svg" "$ICONS/deckport.svg"

cat > "$APPS/deckport.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DeckPort
Comment=Update AppImages and PC ports from GitHub Releases
Exec=python3 $DEST/deckport.py serve --window --exit-when-closed
Icon=deckport
Terminal=false
Categories=Utility;
EOF
chmod +x "$APPS/deckport.desktop"

# Desktop shortcut too, if there is a Desktop folder.
if [ -d "$HOME/Desktop" ]; then
  cp "$APPS/deckport.desktop" "$HOME/Desktop/deckport.desktop"
  chmod +x "$HOME/Desktop/deckport.desktop"
fi

command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true

echo "DeckPort installed."
echo "  Open it from the app menu (Utilities > DeckPort) or the Desktop icon."
echo "  CLI: $BIN/deckport --help"
