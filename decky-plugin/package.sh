#!/usr/bin/env bash
# Build the Decky plugin and zip it for Decky's "Install plugin from ZIP".
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
OUT="${1:-$HERE/out}"
VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/deckport.py")"

cd "$HERE"
[ -d node_modules ] || npm ci --no-audit --no-fund
npm run build

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
P="$STAGE/DeckPort"
mkdir -p "$P/dist" "$P/py_modules" "$OUT"
cp plugin.json main.py "$P/"
# Keep the plugin's version in step with deckport.py.
sed "s/\"version\": \"[^\"]*\"/\"version\": \"$VERSION\"/" package.json > "$P/package.json"
cp dist/index.js "$P/dist/"
cp "$ROOT/deckport.py" "$P/py_modules/"

ZIP="$OUT/DeckPort-decky-$VERSION.zip"
rm -f "$ZIP"
(cd "$STAGE" && zip -qr "$ZIP" DeckPort)
echo "Built $ZIP"
