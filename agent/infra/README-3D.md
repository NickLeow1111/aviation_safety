# 3D aerodrome reconstruction (`blender.py`)

Turns an extracted 2D layout (GeoJSON layers) into a 3D miniature aerodrome:
`airport.blend`, `airport.glb`, `airport_metadata.json`, `reconstruction_report.md`
and `preview.png` (STEP-7 output list from the reconstruction spec).

## Run it

```bash
# 1. a layout to build (demo generator, writes render_out/airport_features.geojson)
source agent/.venv/bin/activate
python agent/infra/make_demo_layout.py

# 2. render (Blender's own interpreter, EEVEE, headless via xvfb)
agent/infra/render_aerodrome.sh render_out/airport_features.geojson render_out --preview
```

Outputs land in the second argument. A typical run is ~10 minutes on this CPU —
almost all of it is the 32-sample EEVEE frame at 1600×1200. Drop `--preview` for a
fast build-only pass (~20 s), which still writes `.blend`, `.glb`, metadata and report.

## Why the invocation looks like that

- **`xvfb-run` is required.** EEVEE needs a display even with `--background`;
  without it Blender aborts with `Unable to open a display` (exit 134) *after*
  writing the blend and glb, so the failure looks like a successful build.
- **Never `import bpy` in the venv.** The script is executed by Blender's bundled
  Python 3.10; the venv's Python 3.11 is only for the extraction side.
- **Cycles is not an option here.** 32 samples takes ~30 minutes/frame on this CPU
  versus ~9 for EEVEE. If lighting fidelity ever needs Cycles, render on a GPU box.

## Input schema

`{"type":"FeatureCollection","features":[…]}`; each feature:

| field | meaning |
|---|---|
| `type` | `runway`, `taxiway`, `apron`, `building`, `hangar`, `tower`, `road`, `perimeter`, `nav_aid`, `aircraft` |
| `name` / `id` | carried into the metadata |
| `width_m` | surface width for line features (runway default 60 m, taxiway 23 m) |
| `height_m` | building height (falls back to 10/15/40 m by type, flagged as assumed) |
| `label` | text drawn in the scene at the feature centroid |
| `confidence` | `extracted` / `inferred` / `assumed` — copied to the metadata report |
| `heading_deg` | aircraft orientation |

Geometry: `LineString` for runways/taxiways/fences, `Polygon` for aprons and
buildings, `Point` for aircraft stands. Coordinates may be lon/lat (projected to
local metres around the layout centroid) or already-local metres.

## Design conventions (and why they differ from true scale)

- **Vertical exaggeration ×6** for buildings, **~2.7× aircraft size**. The spec asks
  for a *miniature* isometric twin; at 1:1 a 22 m terminal beside a 4 km runway is
  a sliver from a 2 km-high ortho camera. Horizontal geometry is never distorted.
- **Camera fits the projected extent**, not a square box — a plain `ortho_scale`
  centred on the scene clips runways that sit off the diagonal.
- **Centreline dashes** are 30 m stripe / 20 m gap along the runway axis.

## Honest limitations

1. **No runway designators, thresholds or touchdown-zone markings yet** — only
   centreline dashes. STEP 4A is ~30 % implemented.
2. **Buildings are extrusions of footprints** — no roofs, doors, windows, canopies.
3. **Taxiway curbs, holding positions and stand lead-in lines are absent.**
4. **Geometry is only as good as the extraction**; nothing here re-derives scale.
5. **No DXF/DWG input** — that needs the ODA File Converter (EULA, not installed).
6. **Precision framing:** the camera fits the model's geometry, so a very wide fence
   line will zoom the airport out. Pass a layout without a perimeter if you want a
   tighter hero shot.
