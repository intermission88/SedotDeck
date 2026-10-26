#!/usr/bin/env bash
# Build the Decky plugin and zip it for Decky's "Install plugin from ZIP".
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
OUT="${1:-$HERE/out}"
mkdir -p "$OUT" && OUT="$(cd "$OUT" && pwd)"  # absolute: the script changes directory
VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/sedotdeck.py")"

cd "$HERE"
[ -d node_modules ] || npm ci --no-audit --no-fund
npm run build

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
P="$STAGE/SedotDeck"
mkdir -p "$P/dist" "$P/py_modules" "$OUT"
cp plugin.json main.py "$ROOT/LICENSE" "$P/"
# Keep the plugin's version in step with sedotdeck.py.
sed "s/\"version\": \"[^\"]*\"/\"version\": \"$VERSION\"/" package.json > "$P/package.json"
cp dist/index.js "$P/dist/"
cp "$ROOT/sedotdeck.py" "$P/py_modules/"

ZIP="$OUT/SedotDeck-decky-$VERSION.zip"
rm -f "$ZIP"
(cd "$STAGE" && zip -qr "$ZIP" SedotDeck)
echo "Built $ZIP"
