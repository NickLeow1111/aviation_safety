#!/usr/bin/env bash
# Render a 3D aerodrome from an extracted GeoJSON layout.
#
#   agent/infra/render_aerodrome.sh <features.geojson> <out-dir> [--preview]
#
# Blender must run its own interpreter — never the venv/Jupyter one. EEVEE needs a
# display even in background mode, so everything goes through xvfb-run.
set -euo pipefail

FEATURES="${1:?usage: render_aerodrome.sh <features.geojson> <out-dir> [--preview]}"
OUTDIR="${2:?usage: render_aerodrome.sh <features.geojson> <out-dir> [--preview]}"
shift 2 || true

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$OUTDIR"

exec xvfb-run -a blender --background \
  --python "$HERE/blender.py" -- \
  --features "$FEATURES" --out-dir "$OUTDIR" "$@"
