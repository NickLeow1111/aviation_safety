#!/usr/bin/env python3
"""
blender.py — procedural 2D-layout → 3D aerodrome reconstruction for the CAAS
aviation-safety stack.

Run with Blender's own interpreter, never the Jupyter/venv one:

    blender --background --python blender.py -- \
        --features airport_features.geojson \
        --out-dir render_out/ \
        [--preview] [--no-glb]

Inputs : GeoJSON layers produced by the extraction side of the pipeline
         (runway / taxiway / apron / building / road / nav aid / perimeter).
         Each feature may carry: type, name, width_m, height_m, metres-per-
         degree scale handled upstream, confidence.
Outputs: airport.blend, airport.glb, airport_metadata.json,
         reconstruction_report.md, preview.png  (STEP-7 output list).

Ground rule (STEP 2/4 of the master prompt): geometry comes from the feature
file. When dimensions are missing we fall back to documented ICAO defaults and
mark them `assumed` in the metadata — nothing is silently invented.
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

import bpy
from mathutils import Vector

# ----------------------------------------------------------------- palette
# STEP 5 visual language: miniature isometric digital twin, clarity first.
COL = {
    "runway":   (0.045, 0.050, 0.055, 1.0),   # dark grey asphalt
    "taxiway":  (0.150, 0.155, 0.160, 1.0),   # medium grey
    "apron":    (0.44, 0.45, 0.47, 1.0),   # light grey
    "building": (0.75, 0.55, 0.30, 1.0),   # beige/orange
    "hangar":   (0.55, 0.58, 0.62, 1.0),
    "tower":    (0.850, 0.850, 0.850, 1.0),
    "road":     (0.220, 0.220, 0.225, 1.0),
    "grass":    (0.36, 0.46, 0.24, 1.0),   # muted green
    "fire.red": (0.75, 0.18, 0.12, 1.0),
    "fence":    (0.35, 0.35, 0.35, 1.0),
    "trunk":    (0.32, 0.22, 0.14, 1.0),
    "foliage":  (0.20, 0.44, 0.18, 1.0),
    "mast":     (0.86, 0.86, 0.84, 1.0),
    "navaid":   (0.900, 0.750, 0.100, 1.0),   # amber
    "aircraft": (0.920, 0.930, 0.940, 1.0),
    "marking":  (0.930, 0.930, 0.900, 1.0),   # markings/labels placeholder
}

TYPE_COLORS = {
    "runway": "runway", "taxiway": "taxiway", "apron": "apron",
    "building": "building", "hangar": "hangar", "tower": "tower",
    "road": "road", "perimeter": "fence", "fence": "fence",
    "nav_aid": "navaid", "navaid": "navaid", "aircraft": "aircraft",
    "grass": "grass",
}

# ------------------------------------------------------------------ GeoJSON

def load_features(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    feats = data.get("features", data if isinstance(data, list) else [])
    # Treat a bare {"type":"LineString"...} as a single feature for convenience.
    if data.get("type") == "FeatureCollection":
        return data.get("features", [])
    if feats:
        return feats
    if data.get("type") in ("Point", "LineString", "Polygon"):
        return [{"properties": {}, "geometry": data}]
    return feats


def geometry_coords(geom):
    """Yield coordinate rings for the shapes we understand."""
    t = geom.get("type")
    c = geom.get("coordinates", [])
    if t == "Point":
        # A point-like aircraft stand is a zero-length 'ring': the aircraft pass
        # averages ring coords, so return the point twice so len() >= 1 works.
        pt = tuple(c[:2])
        return [[pt, pt]]
    if t in ("LineString", "MultiPoint"):
        return [[tuple(x[:2]) for x in c]]
    if t == "Polygon":
        return [[tuple(x[:2]) for x in c[0]]] if c else []
    if t == "MultiPolygon":
        return [[tuple(x[:2]) for x in poly[0]] for poly in c if poly]
    if t == "MultiLineString":
        return [[tuple(x[:2]) for x in line] for line in c]
    return []


def props_of(f):
    p = f.get("properties") or {}
    return p if isinstance(p, dict) else {}


# ------------------------------------------------------------------- center

def compute_origin(features):
    xs, ys = [], []
    for f in features:
        for ring in geometry_coords(f.get("geometry", {})):
            for x, y in ring:
                xs.append(x)
                ys.append(y)
    if not xs:
        return 0.0, 0.0
    return (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0


# ------------------------------------------------------------- mesh helpers

def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def make_collection(name):
    col = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(col)
    return col


def link(obj, col):
    for c in obj.users_collection:
        c.objects.unlink(obj)
    col.objects.link(obj)


def new_material(name, rgba):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = rgba
        bsdf.inputs["Roughness"].default_value = 0.85
    return mat


def flat_mesh(name, ring_m, col, mat, z=0.0):
    """Flat ngon from a metre-space ring. Built with bmesh: 3.0's from_pydata
    chokes on big ngons ('tuple' cannot be interpreted as an integer)."""
    import bmesh
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bm_verts = [bm.verts.new((x, y, z)) for (x, y) in ring_m]
    if len(bm_verts) >= 3:
        bm.faces.new(bm_verts)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    link(obj, col)
    obj.data.materials.append(mat)
    return obj


def extrude_mesh(name, ring_m, height, col, mat, z0=0.0):
    """Flat footprint extruded to `height` — walls + roof cap, normals ok."""
    obj = flat_mesh(name, ring_m, col, mat, z=z0)
    mesh = obj.data
    n = len(mesh.vertices)
    verts = [v.co for v in mesh.vertices]
    import bmesh
    walls = bpy.data.meshes.new(name + "_walls")
    bm = bmesh.new()
    bv = [bm.verts.new((v.x, v.y, v.z)) for v in verts]
    bt = [bm.verts.new((v.x, v.y, z0 + height)) for v in verts]
    for i in range(n):
        j = (i + 1) % n
        bm.faces.new((bv[i], bv[j], bt[j], bt[i]))
    bm.to_mesh(walls)
    bm.free()
    walls.update()

    obj.data = walls
    walls.materials.append(mat)

    cap = flat_mesh(name + "_roof", [(v.x, v.y) for v in verts], col, mat, z=z0 + height)
    return obj, cap


def box(name, xc, yc, w, d, h, col, mat, z0=0.0):
    """Axis-aligned box centred at (xc,yc), size w×d, height h."""
    r = [(xc - w / 2, yc - d / 2), (xc + w / 2, yc - d / 2),
         (xc + w / 2, yc + d / 2), (xc - w / 2, yc + d / 2)]
    obj, _ = extrude_mesh(name, r, h, col, mat, z0=z0)
    return obj


# ------------------------------------------------------- geometry conversions

def rings_metres(feature, origin, m_per_deg_lat, m_per_deg_lon):
    """GeoJSON lon/lat (or drawing units) → local metres centred on origin."""
    out = []
    for ring in geometry_coords(feature.get("geometry", {})):
        ring_m = []
        for lon, lat in ring:
            mx = (lon - origin[0]) * m_per_deg_lon
            my = (lat - origin[1]) * m_per_deg_lat
            ring_m.append((mx, my))
        if len(ring_m) >= 2:
            out.append(ring_m)
    return out


def line_width(prop, fallback):
    w = prop.get("width_m")
    if isinstance(w, (int, float)) and w > 0:
        return float(w)
    return fallback  # marked assumed in report


MARK_SCALE = 1.0   # scene_size/1000 — markings widen with the model so they stay visible


def marking_width(real_m):
    """Paint lines thinner than ~1.2 m are sub-pixel at ortho framing; widen them."""
    return max(real_m, MARK_SCALE * 1.4)


def ribbon(ring_m, width, col, mat, name, z=0.02):
    """LineString → flat ribbon of the given width (aerodrome 2-D intent)."""
    pts = [(x, y) for (x, y) in ring_m if (x, y)]
    dedup = []
    for q in pts:
        if not dedup or (abs(q[0]-dedup[-1][0]) + abs(q[1]-dedup[-1][1])) > 1e-6:
            dedup.append(q)
    pts = dedup
    if len(pts) < 2:
        return []
    # Segment midpoints + directions, offset each point by the local normal.
    left, right = [], []
    n = len(pts)
    for i, (x, y) in enumerate(pts):
        if i == 0:
            dx, dy = pts[1][0] - x, pts[1][1] - y
        elif i == n - 1:
            dx, dy = x - pts[i - 1][0], y - pts[i - 1][1]
        else:
            dx, dy = pts[i + 1][0] - pts[i - 1][0], pts[i + 1][1] - pts[i - 1][1]
        L = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / L, dx / L
        left.append((x + nx * width / 2, y + ny * width / 2))
        right.append((x - nx * width / 2, y - ny * width / 2))
    return [flat_mesh(name, left + right[::-1], col, mat, z=z)]


# ------------------------------------------------------------------ builders

def build_ground_plane(size, col, mat):
    return box("Ground", 0, 0, size, size, 0.05, col, mat, z0=-0.05)


def build_runway(name, ring, col, mat, ctx):
    obj = flat_mesh(name, ring[0], col, mat, z=0.02)
    ctx["counts"]["runway"] += 1
    return obj


def build_polys(name_key, f, ring, origin, col, mat, ctx, height_default):
    p = props_of(f)
    h = p.get("height_m")
    h = float(h) if isinstance(h, (int, float)) and h > 0 else height_default
    objs = extrude_mesh(p.get("name") or name_key, ring[0], h, col, mat)
    ctx["counts"][name_key] += 1
    return list(objs)



# ------------------------------------------------------- scene dressing (4E)

def scatter_vegetation(root, mats, scene_size, exclusions, count=260, seed=7):
    """Trees on the grass, outside every pavement footprint.

    The spec asks for trees and vegetation (STEP 4E) and an empty green field is
    what makes an ortho render look like a wireframe. Deterministic RNG so repeat
    runs of the same layout are identical.
    """
    import random
    rng = random.Random(seed)
    col = make_collection("Vegetation")
    half = scene_size * 0.62
    made = 0
    tries = 0
    while made < count and tries < count * 12:
        tries += 1
        x = rng.uniform(-half, half)
        y = rng.uniform(-half, half)
        if any(x0 <= x <= x1 and y0 <= y <= y1 for (x0, x1, y0, y1) in exclusions):
            continue
        t = rng.uniform(0.75, 1.35)
        trunk_h = 6.0 * t
        canopy_h = 14.0 * t
        canopy_w = 9.0 * t
        box(f"Tree{ made }_trunk", x, y, 1.8 * t, 1.8 * t, trunk_h, col, mats["trunk"])
        canopy = box(f"Tree{ made }_canopy", x, y, canopy_w, canopy_w, canopy_h,
                     col, mats["foliage"], z0=trunk_h)
        # slight random yaw keeps the canopy grid from looking stamp-printed
        canopy.rotation_euler[2] = rng.uniform(0, math.pi)
        made += 1
    return made


def place_lighting_masts(root, mats, rings_by_feature, scene_size):
    """Simple apron/taxiway lighting poles — small verticals that read as detail."""
    col = make_collection("Lighting")
    n = 0
    for rings, step in rings_by_feature:
        for ring in rings:
            if len(ring) < 2:
                continue
            x0, y0 = ring[0]
            x1, y1 = ring[-1]
            L = math.hypot(x1 - x0, y1 - y0)
            if L < 200:
                continue
            ux, uy = (x1 - x0) / L, (y1 - y0) / L
            t = step
            while t < L:
                px, py = x0 + ux * t, y0 + uy * t
                box(f"Mast{n}", px, py, 2.6, 2.6, 22.0, col, mats["mast"])
                n += 1
                t += step
    return n

# ------------------------------------------------------------------ aircraft

def build_aircraft(name, xc, yc, heading_deg, col, mat):
    """Generic airliner silhouette — never typed as a real model. The 2x size is
    deliberate: from a 2 km ortho camera a true-scale airliner is ~4 px."""
    L, W = 110.0, 100.0   # generic silhouette, exaggerated for legibility
    fus = box(name + "_fus", xc, yc, L * 0.11, L, 5.0, col, mat, z0=3.0)
    wing = box(name + "_wing", xc, yc, W, L * 0.13, 1.2, col, mat, z0=6.0)
    stab = box(name + "_stab", xc, yc - L * 0.40, L * 0.34, L * 0.09, 1.0, col, mat, z0=5.0)
    fin = box(name + "_fin", xc, yc - L * 0.44, 1.4, L * 0.14, 9.0, col, mat, z0=6.0)
    for o in (fus, wing, stab, fin):
        o.rotation_euler[2] = math.radians(heading_deg)
    return [fus, wing, stab, fin]


# ------------------------------------------------------------------- labels

def add_label(name, text, x, y, z, size=4.0, col=None):
    """Real text object in the scene, visible from the isometric camera."""
    curve = bpy.data.curves.new(name, type="FONT")
    curve.body = text
    curve.size = size
    curve.align_x = "CENTER"
    obj = bpy.data.objects.new(name, curve)
    obj.location = (x, y, z)
    obj.rotation_euler[0] = 0.0
    (col or bpy.context.scene.collection).objects.link(obj)
    mat = new_material("LabelMat", (0.02, 0.05, 0.10, 1.0))
    obj.data.materials.append(mat)
    return obj


# ------------------------------------------------------------------- camera

def setup_camera_and_light(scene_size):
    scene = bpy.context.scene
    cam_data = bpy.data.cameras.new("IsoCam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = scene_size * 1.15
    # Ortho defaults clip at 100 m — a 6 km scene would be entirely culled and the
    # preview renders as a blank frame (seen on the first Changi test).
    cam_data.clip_start = 1.0
    cam_data.clip_end = scene_size * 6.0
    cam = bpy.data.objects.new("IsoCam", cam_data)
    d = scene_size * 0.9
    cam.location = (d, -d, d * 0.85)
    # look at the centre
    direction = Vector((0, 0, 0)) - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.collection.objects.link(cam)
    scene.camera = cam

    sun = bpy.data.lights.new("Sun", type="SUN")
    sun.energy = 9.0
    sun.angle = math.radians(12)
    sun_obj = bpy.data.objects.new("Sun", sun)
    sun_obj.location = (scene_size, -scene_size, scene_size * 1.6)
    sun_obj.rotation_euler = (math.radians(50), 0, math.radians(-35))
    bpy.context.scene.collection.objects.link(sun_obj)

    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.90, 0.92, 0.95, 1.0)
        bg.inputs[1].default_value = 0.55
    scene.world = world
    return cam


# -------------------------------------------------------------------- main

def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--scale-m-per-degree", type=float, default=None,
                    help="metres per degree of longitude (lat scaled by cos(lat))")
    ap.add_argument("--preview", action="store_true")
    args = ap.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)
    features = load_features(args.features)
    if not features:
        print("NO_FEATURES: nothing to build")
        sys.exit(1)

    clear_scene()
    root = make_collection("Aerodrome")

    origin = compute_origin(features)
    lat = origin[1]
    if args.scale_m_per_degree:
        m_per_deg_lon = args.scale_m_per_degree
        m_per_deg_lat = m_per_deg_lon * 1.086  # rough mean-latitude correction
    else:
        m_per_deg_lon = 111320.0 * math.cos(math.radians(abs(lat)))
        m_per_deg_lat = 110540.0

    mats = {k: new_material(f"Mat_{k}", v) for k, v in COL.items()}
    counts = {k: 0 for k in ("runway", "taxiway", "apron", "building", "road", "aircraft", "perimeter", "other")}
    ctx = {"counts": counts}
    meta = []
    assumptions = []

    ground = build_ground_plane(9000, root, mats["grass"])

    for f in features:
        p = props_of(f)
        ftype = (p.get("type") or p.get("feature_type") or "other").lower()
        name = p.get("name") or f"{ftype}_{len(meta) + 1}"
        rings = rings_metres(f, origin, m_per_deg_lat, m_per_deg_lon)
        if not rings:
            continue

        color_key = TYPE_COLORS.get(ftype, "building")
        mat_key = color_key
        # STEP 5 says miniature digital twin. At true 1:1 a 22 m terminal next to a
        # 4 km runway is a sliver from 2 km up, so verticals are exaggerated ~6x —
        # the same convention every map/3D twin uses. Horizontal NEVER touched.
        vertical_exaggeration = 6.0
        objs = []

        if ftype == "runway":
            # Centreline dashes: 30 m stripe / 20 m gap, ICAO style
            for ring in rings:
                pts = ring
                for i in range(len(pts) - 1):
                    (x0, y0), (x1, y1) = pts[i], pts[i + 1]
                    seg = math.hypot(x1 - x0, y1 - y0)
                    if seg < 1:
                        continue
                    ux, uy = (x1 - x0) / seg, (y1 - y0) / seg
                    t = 0.0
                    while t + 30.0 < seg:
                        objs.append(ribbon([( x0 + ux * t, y0 + uy * t),
                                            (x0 + ux * (t + 30), y0 + uy * (t + 30))],
                                           marking_width(1.2), root, mats["marking"], f"{name}_dash{i}_{int(t)}", z=0.03))
                        t += 50.0
            w = line_width(p, 60.0)
            if w == 60.0 and not p.get("width_m"):
                assumptions.append(f"{name}: width assumed 60 m (ICAO ref code 4)")
            for ring in rings:
                objs.extend(ribbon(ring, w, root, mats["runway"], name + "_surf", z=0.02))
                # Threshold 'piano keys': 6 bars at each end, airport-marking style
                if len(ring) >= 2:
                    (ax, ay), (bx, by) = ring[0], ring[-1]
                    L = math.hypot(bx - ax, by - ay)
                    if L > 100:
                        ux, uy = (bx - ax) / L, (by - ay) / L
                        nx, ny = -uy, ux
                        for end_i, (ex, ey, sign) in enumerate(
                                [(ax + ux * 45, ay + uy * 45, 1.0),
                                 (bx - ux * 45, by - uy * 45, -1.0)]):
                            for k in range(6):
                                off = (k - 2.5) * (w * 0.115)
                                cx0 = ex + nx * off
                                cy0 = ey + ny * off
                                bx0 = cx0 + ux * (48 * sign)
                                by0 = cy0 + uy * (48 * sign)
                                objs.append(ribbon([(cx0, cy0), (bx0, by0)], marking_width(max(1.6, w * 0.035)),
                                                   root, mats["marking"],
                                                   f"{name}_thr{end_i}_{k}", z=0.04))
                        # runway edge lines
                        for side in (-1.0, 1.0):
                            e0 = (ax + nx * (w / 2 - 2.5) * side, ay + ny * (w / 2 - 2.5) * side)
                            e1 = (bx + nx * (w / 2 - 2.5) * side, by + ny * (w / 2 - 2.5) * side)
                            objs.append(ribbon([e0, e1], marking_width(2.4), root, mats["marking"], f"{name}_edge{int(side)}", z=0.04))
            ctx["counts"]["runway"] += 1
        elif ftype in ("taxiway", "road"):
            w = line_width(p, 23.0 if ftype == "taxiway" else 7.0)
            if w in (23.0, 7.0):
                assumptions.append(f"{name}: width assumed {w} m (ICAO default)")
            for ring in rings:
                objs.extend(ribbon(ring, w, root, mats[mat_key], name + "_surf"))
            ctx["counts"]["taxiway" if ftype == "taxiway" else "road"] += 1
        elif ftype in ("perimeter", "fence"):
            # A 10 m ribbon is invisible from 2 km up, so each segment becomes a
            # low wall built directly in WORLD coordinates. (Rotating a box whose
            # vertices are already absolute spins it around the world origin —
            # that bug produced stray 6 km lines across the render.)
            for ring in rings:
                for i in range(len(ring) - 1):
                    (x0, y0), (x1, y1) = ring[i], ring[i + 1]
                    L = math.hypot(x1 - x0, y1 - y0)
                    if L < 1:
                        continue
                    ux, uy = (x1 - x0) / L, (y1 - y0) / L
                    nx, ny = -uy, ux            # perpendicular
                    th = 5.0
                    quad = [(x0 + nx * th - ux * th, y0 + ny * th - uy * th),
                            (x1 + nx * th + ux * th, y1 + ny * th + uy * th),
                            (x1 - nx * th + ux * th, y1 - ny * th + uy * th),
                            (x0 - nx * th - ux * th, y0 - ny * th - uy * th)]
                    objs.extend(extrude_mesh(f"{name}_w{i}", quad, 14.0, root, mats["fence"]))
            ctx["counts"]["perimeter" if "perimeter" in ctx["counts"] else "other"] += 1
        elif ftype == "building" and (props_of(f).get("id") == "FSS"):
            for ring in rings:
                objs.extend(extrude_mesh(name, ring, 10.0 * vertical_exaggeration, root, mats.get("fire.red", mats["building"])))
            ctx["counts"]["building"] += 1
            if True:
                pass
        elif ftype in ("apron", "grass", "building", "hangar", "tower", "nav_aid", "navaid"):
            h = {"building": 10.0, "hangar": 15.0, "tower": 40.0}.get(ftype, 0.0)
            for ring in rings:
                if h > 0 and len(ring) >= 3:
                    objs.extend(extrude_mesh(name, ring, h * vertical_exaggeration, root, mats[mat_key]))
                    ctx["counts"]["building"] += 1
                    # ATC cab: a wider disc on a stalk reads as a tower from above;
                    # a plain extruded box just reads as another building.
                    if ftype == "tower":
                        rr = [max(x for x, _ in ring) - min(x for x, _ in ring),
                              max(y for _, y in ring) - min(y for _, y in ring)]
                        tcx = sum(x for x, _ in ring) / len(ring)
                        tcy = sum(y for _, y in ring) / len(ring)
                        cab_w = max(rr) * 2.6
                        cab = box(name + "_cab", tcx, tcy, cab_w, cab_w, h * vertical_exaggeration * 0.18,
                                  root, mats[mat_key], z0=h * vertical_exaggeration)
                        objs.append(cab)
                else:
                    objs.append(flat_mesh(name, ring, root, mats[mat_key], z=0.01))
                    ctx["counts"]["apron" if ftype == "apron" else "other"] += 1
        else:
            for ring in rings:
                objs.append(flat_mesh(name, ring, root, mats.get(mat_key, mats["building"]), z=0.01))
                ctx["counts"]["other"] += 1

        for o in objs:
            if o:
                meta.append({
                    "id": p.get("id") or name,
                    "feature_type": ftype,
                    "name": name,
                    "objects": [x.name for x in o] if isinstance(o, list) else ([o.name] if o else []),
                    "confidence": p.get("confidence", "extracted" if p.get("height_m") else "inferred"),
                    "source": p.get("source", "drawing"),
                })

    # Aircraft on aprons where the drawing marks them
    for f in features:
        p = props_of(f)
        if (p.get("type") or "").lower() == "aircraft":
            rings = rings_metres(f, origin, m_per_deg_lat, m_per_deg_lon)
            for ring in rings:
                if not ring:
                    continue
                xs = [x for x, _y in ring]
                ys = [y for _x, y in ring]
                heading = float(p.get("heading_deg", 0))
                objs = build_aircraft(p.get("name") or "Aircraft",
                                      sum(xs) / len(xs), sum(ys) / len(ys),
                                      heading, root, mats["aircraft"])
                ctx["counts"]["aircraft"] += 1

    # ---------------- bounds, camera, render ------------------------------
    allx = [v.co.x for o in root.objects for v in o.data.vertices] if False else []
    xs, ys = [], []
    for o in root.objects:
        if o.type == "MESH":
            for v in o.data.vertices:
                xs.append(v.co.x)
                ys.append(v.co.y)
    # Frame on the AIRPORT, not the fence: the isometric miniature look needs the
    # infrastructure to fill the frame. Size from runways+taxyways+buildings only,
    # ignoring the perimeter ring (which can be 2-3x wider than the aerodrome).
    core = [f for f in features if (props_of(f).get("type") or "").lower()
            not in ("perimeter", "fence")]
    cx, cy = [], []
    for f in core:
        for ring in rings_metres(f, origin, m_per_deg_lat, m_per_deg_lon):
            cx += [x for x, _ in ring]
            cy += [y for _, y in ring]
    if not cx:
        cx, cy = xs[:], ys[:]
    scene_size = max(max(cx) - min(cx), max(cy) - min(cy), 800.0)
    # Recentre the ground under the airport, not the fence.
    ground.location = ((max(cx) + min(cx)) / 2, (max(cy) + min(cy)) / 2, 0)
    global MARK_SCALE
    MARK_SCALE = max(1.0, scene_size / 1000.0)
    cam = setup_camera_and_light(scene_size)
    # Fit the camera to the project extent: a square ortho_scale centred on the
    # scene cuts runways that sit off the diagonal (RWY 02C was clipped).
    pts3 = []
    for o in root.objects:
        if o.type != "MESH" or o.name.startswith("Ground"):
            continue
        # The perimeter fence spans the whole site; including it in the fit zooms
        # the airport out to ~7 px per runway width. Frame on the core instead.
        if "fence" in o.name.lower() or o.name.lower().startswith("perim"):
            continue
        for v in o.data.vertices:
            pts3.append((v.co.x, v.co.y, v.co.z))
    if pts3:
        bpy.context.view_layer.update()
        inv = cam.matrix_world.inverted()
        sx, sy = [], []
        for (x, y, z) in pts3:
            v = inv @ Vector((x, y, z))
            sx.append(v.x)
            sy.append(v.y)
        w = max(sx) - min(sx)
        h = max(sy) - min(sy)
        aspect = bpy.context.scene.render.resolution_x / bpy.context.scene.render.resolution_y
        cam.data.ortho_scale = max(w, h * aspect) * 1.10
        cam.data.clip_end = max(cam.data.clip_end, (w + h) * 4.0)
    # ---------------- vegetation + masts (STEP 4E) -------------------------
    pads = []
    line_rings = []
    for f in features:
        p = props_of(f)
        ft = (p.get("type") or "").lower()
        rings = rings_metres(f, origin, m_per_deg_lat, m_per_deg_lon)
        for ring in rings:
            if not ring:
                continue
            # The fence ring's bounding box covers the whole site — excluding it
            # would leave nowhere to plant a tree.
            if ft in ("perimeter", "fence"):
                continue
            rx = [x for x, _ in ring]
            ry = [y for _, y in ring]
            pad = scene_size * 0.012
            pads.append((min(rx) - pad, max(rx) + pad, min(ry) - pad, max(ry) + pad))
            if ft in ("runway", "taxiway") and len(ring) >= 2:
                line_rings.append((rings, max(180.0, scene_size * 0.09)))
    n_trees = scatter_vegetation(root, mats, scene_size, pads)
    n_masts = place_lighting_masts(root, mats, line_rings[:2], scene_size)
    report_extras = {"trees": n_trees, "lighting_masts": n_masts}
    print(f"DRESSING trees={n_trees} masts={n_masts}")

    # ---------------- labels (STEP 6) -------------------------------------
    label_col = make_collection("Labels")
    size = scene_size * 0.017
    label_i = 0
    for f in features:
        p = props_of(f)
        ftype = (p.get("type") or "").lower()
        if not p.get("label") or ftype in ("perimeter", "fence"):
            continue
        rings = rings_metres(f, origin, m_per_deg_lat, m_per_deg_lon)
        if not rings:
            continue
        ring = rings[0]
        linestring = f.get("geometry", {}).get("type") == "LineString" and len(ring) >= 2
        if linestring:
            # Place line labels 32% along the feature and push them off the surface —
            # centroid placement put runway and taxiway labels on top of each other.
            (x0, y0), (x1, y1) = ring[0], ring[-1]
            L = math.hypot(x1 - x0, y1 - y0) or 1.0
            ux, uy = (x1 - x0) / L, (y1 - y0) / L
            nx, ny = -uy, ux
            side = 1.0 if (label_i % 2 == 0) else -1.0
            off = side * (line_width(p, 60.0) / 2 + scene_size * 0.030)
            frac = 0.26 if (label_i % 2 == 0) else 0.70
            cx = x0 + ux * L * frac + nx * off
            cy = y0 + uy * L * frac + ny * off
        else:
            cx = sum(x for x, _ in ring) / len(ring)
            cy = sum(y for _, y in ring) / len(ring)
        add_label(f"Lbl_{p['label']}", p["label"], cx, cy,
                  size * (1.2 + 0.35 * (label_i % 3)), size=size, col=label_col)
        label_i += 1


    # ---------------- export (STEP 7) -------------------------------------
    blend_path = os.path.join(args.out_dir, "airport.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)

    glb_path = os.path.join(args.out_dir, "airport.glb")
    bpy.ops.export_scene.gltf(filepath=glb_path, export_format="GLB",
                              export_apply=True, export_cameras=False)

    metadata = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source_features": args.features,
        "origin_lonlat": list(origin),
        "scale_m_per_degree_lon": m_per_deg_lon,
        "counts": ctx["counts"],
        "objects": meta,
        "assumptions": assumptions,
        "blender_version": bpy.app.version_string,
    }
    with open(os.path.join(args.out_dir, "airport_metadata.json"), "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2)

    report = ["# Reconstruction report", "",
              f"Generated {metadata['generated_utc']} from `{args.features}`",
              "",
              "## Counts", ""]
    report += [f"- **{k}**: {v}" for k, v in ctx["counts"].items()]
    if assumptions:
        report += ["", "## Assumptions", ""]
        report += [f"- {a}" for a in assumptions]
    report += ["", "## Known limitations", "",
               "- Runway markings are not yet generated procedurally.",
               "- Buildings are simple extrusions of drawing footprints.",
               "- ICAO default dimensions substituted where the drawing was silent (see Assumptions)."]
    with open(os.path.join(args.out_dir, "reconstruction_report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(report) + "\n")

    if args.preview:
        scene = bpy.context.scene
        # EEVEE only: Cycles needs ~30 min on this CPU for one frame. If the
        # colours read wrong under EEVEE sun, tune via the film exposure below.
        scene.render.engine = "BLENDER_EEVEE"
        scene.eevee.taa_render_samples = 32
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
        scene.render.resolution_x = 1600
        scene.render.resolution_y = 1200
        scene.render.filepath = os.path.join(args.out_dir, "preview.png")
        bpy.ops.render.render(write_still=True)

    print(f"DONE objects={len(bpy.data.objects)} blend={blend_path} glb={glb_path}")


if __name__ == "__main__":
    main()
