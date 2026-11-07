#!/usr/bin/env python3
"""
make_demo_layout.py — synthetic Changi-style aerodrome layout, written to the
same GeoJSON schema `blender.py` consumes. Real WSSS runway dimensions (02L/20R
4,000 m × 60 m — published data), aprons/stands/hangar placed to scale.

Pure stdlib + the venv's json — no network, no invented survey data: the
runway numbers are documented dimensions and everything else is explicitly a
test layout, not a claim about the real airfield.
"""

import json
import math
import os

# WSSS reference point 1.36N 103.99E (published ARP). Runway 02L/20R bearing
# ≈ 023.5° true. We work in local metres and convert BACK to lon/lat offsets.
ARP_LON, ARP_LAT = 103.9917, 1.3644
M_PER_DEG_LON = 111320.0 * math.cos(math.radians(1.3644))
M_PER_DEG_LAT = 110540.0

def to_lonlat(mx, my):
    return [ARP_LON + mx / M_PER_DEG_LON, ARP_LAT + my / M_PER_DEG_LAT]

def ring_from_metres(metres):
    return [to_lonlat(x, y) for x, y in metres]

def line_metres(x0, y0, bearing_deg, length):
    """metres from start along a true bearing."""
    a = math.radians(bearing_deg)
    return [ (x0, y0), (x0 + length * math.sin(a), y0 + length * math.cos(a)) ]

def poly_ring(cx, cy, w, d):
    return [(cx - w/2, cy - d/2), (cx + w/2, cy - d/2),
            (cx + w/2, cy + d/2), (cx - w/2, cy + d/2)]

features = []

# --- Runway 02L/20R: 4000 × 60 m, bearing 023.5° -------------------------
ry_m = line_metres(-900, -1600, 23.5, 4000)
features.append({
    "type": "Feature",
    "properties": {"type": "runway", "id": "RWY-02L20R", "name": "RWY 02L/20R",
                    "label": "RWY 02L/20R", "width_m": 60, "confidence": "extracted",
                    "source": "published dimensions (4,000×60 m)"},
    "geometry": {"type": "LineString", "coordinates": ring_from_metres(ry_m)},
})

# --- parallel runway 02C/20C offset 1,800 m east (schematic test) --------
ry2 = line_metres(900, -1600, 23.5, 4000)
features.append({
    "type": "Feature",
    "properties": {"type": "runway", "id": "RWY-02C20C", "name": "RWY 02C/20C",
                    "label": "RWY 02C/20C", "width_m": 60, "confidence": "assumed"},
    "geometry": {"type": "LineString", "coordinates": ring_from_metres(ry2)},
})

# --- Taxiways: parallel + two connectors ---------------------------------
tw_main = line_metres(-300, -1600, 23.5, 4000)
features.append({
    "type": "Feature",
    "properties": {"type": "taxiway", "id": "TWY-A", "name": "TWY A",
                    "label": "TWY A", "width_m": 25, "confidence": "assumed"},
    "geometry": {"type": "LineString", "coordinates": ring_from_metres(tw_main)},
})
for i, (yy, nm) in enumerate([(-1200, "B1"), (0, "B2"), (1200, "B3")]):
    # connectors stop short of the apron slab so taxiway lines don't cut across it
    length = 1800 if yy < 0 else 1400
    con = line_metres(-900, yy, 23.5 + 90, length)
    features.append({
        "type": "Feature",
        "properties": {"type": "taxiway", "id": f"TWY-{nm}", "name": f"TWY {nm}", "width_m": 25},
        "geometry": {"type": "LineString", "coordinates": ring_from_metres(con)},
    })

# --- Terminal 3 apron: a broad slab NW of the runways --------------------
apron = poly_ring(-300, 900, 1400, 500)
features.append({
    "type": "Feature",
    "properties": {"type": "apron", "id": "APRON-T3", "name": "Apron 3",
                    "label": "Apron 3", "confidence": "assumed"},
    "geometry": {"type": "Polygon", "coordinates": [ring_from_metres(apron)]},
})

# --- Terminal building ----------------------------------------------------
term = poly_ring(-300, 1750, 1600, 220)
features.append({
    "type": "Feature",
    "properties": {"type": "building", "id": "T3", "name": "Terminal 3",
                    "label": "Terminal 3", "height_m": 22, "confidence": "assumed"},
    "geometry": {"type": "Polygon", "coordinates": [ring_from_metres(term)]},
})

# --- Control tower --------------------------------------------------------
twr = poly_ring(400, 1500, 45, 45)
features.append({
    "type": "Feature",
    "properties": {"type": "tower", "id": "ATC", "name": "ATC Tower",
                    "label": "ATC Tower", "height_m": 80, "confidence": "extracted"},
    "geometry": {"type": "Polygon", "coordinates": [ring_from_metres(twr)]},
})

# --- Cargo hangar ---------------------------------------------------------
hgr = poly_ring(-1500, 400, 240, 180)
features.append({
    "type": "Feature",
    "properties": {"type": "hangar", "id": "HGR-1", "name": "Cargo Hangar 1",
                    "label": "Hangar 1", "height_m": 26, "confidence": "assumed"},
    "geometry": {"type": "Polygon", "coordinates": [ring_from_metres(hgr)]},
})

# --- Fire station ---------------------------------------------------------
frc = poly_ring(-1500, -400, 90, 70)
features.append({
    "type": "Feature",
    "properties": {"type": "building", "id": "FSS", "name": "Fire Station",
                    "label": "Fire Station", "height_m": 12, "confidence": "assumed"},
    "geometry": {"type": "Polygon", "coordinates": [ring_from_metres(frc)]},
})

# --- Aircraft stands: generic airliners -------------------------------
for i, (sx, hdg) in enumerate([(-750, 23.5 + 180), (-450, 23.5 + 180), (-150, 23.5 + 180), (150, 23.5 + 180)]):
    mx, my = sx, 900
    features.append({
        "type": "Feature",
        "properties": {"type": "aircraft", "id": f"STD-{i+1}", "name": f"Aircraft Stand {i+1}",
                        "heading_deg": hdg},
        "geometry": {"type": "Point", "coordinates": to_lonlat(mx, my)},
    })

# --- Perimeter fence ------------------------------------------------------
per = [(-3200, -3200), (3200, -3200), (3200, 3200), (-3200, 3200), (-3200, -3200)]
features.append({
    "type": "Feature",
    "properties": {"type": "perimeter", "id": "PERIM", "name": "Perimeter fence"},
    "geometry": {"type": "LineString", "coordinates": ring_from_metres(per)},
})

out = os.path.join(os.path.dirname(__file__), "..", "..", "render_out")
out = os.path.abspath(out)
os.makedirs(out, exist_ok=True)
path = os.path.join(out, "airport_features.geojson")
with open(path, "w") as fh:
    json.dump({"type": "FeatureCollection", "features": features}, fh, indent=1)
print("WROTE", path, len(features), "features")
