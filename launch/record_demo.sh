#!/usr/bin/env bash
# Record the `lumin demo` Show-HN asset, deterministically.
#
# Produces:
#   launch/out/lumin-demo.cast   asciinema recording (source of truth)
#   launch/out/lumin-demo.gif    GIF for the README / social (if agg present)
#
# The point: the GIF must show ONE thing — an agent getting prompt-injected
# and the firewall blocking it live — in ~20s. `lumin demo` already does
# exactly that and nothing else, so the recording is just: clean env →
# start Lumin → record one `lumin demo` run → convert.
#
# Deps (all optional-checked, with install hints):
#   - docker            (to run Lumin)               required
#   - asciinema         (brew install asciinema)     required
#   - agg               (cargo install --git https://github.com/asciinema/agg)
#                                                     optional (cast→gif)
#
# Usage:   bash launch/record_demo.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/launch/out"
CAST="$OUT/lumin-demo.cast"
GIF="$OUT/lumin-demo.gif"
mkdir -p "$OUT"

need() { command -v "$1" >/dev/null 2>&1 || { echo "✗ missing: $1 — $2"; exit 1; }; }
need docker    "install Docker Desktop"
need asciinema "brew install asciinema  (or pipx install asciinema)"

# A throwaway lumin so the recording is always a clean first-run.
echo "▸ starting a clean Lumin (port 8000/3000) …"
docker rm -f lumin-rec >/dev/null 2>&1 || true
docker run -d --name lumin-rec \
  -p 127.0.0.1:3000:3000 -p 127.0.0.1:8000:8000 \
  -v lumin-rec-data:/data zistica/lumin:latest >/dev/null

echo "▸ waiting for health …"
for _ in $(seq 1 120); do
  curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1 && break
  sleep 1
done

# Force colour + a fixed 90x28 geometry so the cast is crisp and the
# panels never wrap. NO_COLOR unset, COLORTERM=truecolor.
export COLORTERM=truecolor
unset NO_COLOR || true

echo "▸ recording  (one clean \`lumin demo\` run) …"
rm -f "$CAST"
asciinema rec "$CAST" \
  --rows 28 --cols 90 --idle-time-limit 1.5 --overwrite \
  --title "Lumin — watch the firewall block a live agent attack" \
  --command "lumin demo --no-open --host http://127.0.0.1:8000"

echo "▸ cleaning up the throwaway container …"
docker rm -f lumin-rec >/dev/null 2>&1 || true
docker volume rm lumin-rec-data >/dev/null 2>&1 || true

if command -v agg >/dev/null 2>&1; then
  echo "▸ converting cast → gif …"
  agg --theme asciinema --speed 1.3 --font-size 20 "$CAST" "$GIF"
  echo "✓ $GIF"
else
  echo "! 'agg' not found — cast saved. Convert with either:"
  echo "    cargo install --git https://github.com/asciinema/agg && \\"
  echo "      agg --speed 1.3 $CAST $GIF"
  echo "    # or upload $CAST to asciinema.org and embed the player"
fi

echo
echo "✓ Done.  Asset: $CAST"
echo "  README embed (GIF):   ![Lumin demo](launch/out/lumin-demo.gif)"
echo "  Show HN:  link the asciinema player + the GIF in the post body."
