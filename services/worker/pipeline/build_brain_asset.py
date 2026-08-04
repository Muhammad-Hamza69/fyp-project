"""
Build the compact brain/vascular asset the dashboard renders.

WHY THIS EXISTS
---------------
`NervesOnly_v1.glb` is 35.7 MB and 902,087 faces. Shipping it took ~70 s to
download and was the direct cause of the "why is the 3D model taking so long to
load" complaint. It also bundles three sub-meshes, two of which the viewer has
always hidden — so roughly a third of those bytes were downloaded and thrown
away on every page load.

This produces `models/brain.glb`: the nerve/vascular network alone, decimated
to a face budget, at a fraction of the size and visually equivalent at screen
resolution.

IT ALSO BAKES THE HOTSPOT
-------------------------
The old viewer located the aneurysm site in the browser by scanning all 453,091
vertices twice on every load (once to find the topmost cluster's centroid, once
to compute a per-vertex falloff). That is a fixed startup cost paid by every
visitor to compute the same constant. It is computed once here and written to
the sidecar instead.

HONESTY NOTE — read before citing this in the report
----------------------------------------------------
This asset is a GENERIC anatomical model. It is not any patient's vasculature,
and nothing in it is derived from a scan. What IS per-patient is the bulge
placed on it: its size comes from morphology measured on that case's
reconstructed surface, and its colour from that case's computed hemodynamics.
So the bulge is data; the brain around it is context. The dashboard must say so
rather than letting a viewer assume the whole picture is patient-specific.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

# The source model is unitless. An adult cerebrum is ~167 mm front-to-back, and
# the asset's longest axis is the same anterior-posterior direction, so that
# fixes the scale. It matters: it is what makes an 11 mm aneurysm render
# visibly larger than a 5 mm one IN PROPORTION rather than by an arbitrary
# artistic factor.
BRAIN_LENGTH_MM = 167.0

# Fraction of the height treated as the "top" cluster whose centroid becomes
# the aneurysm site, matching the original viewer's 0.92.
HOTSPOT_TOP_FRACTION = 0.92

# Anatomical anchors for the aneurysm site, in normalised model units.
#
# ORIENTATION, established by rendering the asset along each axis with marked
# poles rather than assumed: +Y is ANTERIOR (the -Y aspect shows cerebellum and
# brainstem), +Z is SUPERIOR, X is left-right. The asset spans X +/-0.665,
# Y +/-0.9, Z +/-0.815.
#
# This REPLACES the original viewer's rule of "centroid of the topmost cluster
# in Y". Y is this model's anterior-posterior axis, not its vertical one, so
# that rule placed the aneurysm at the frontal pole. Saccular aneurysms arise
# at the Circle of Willis — inferior and central — so every case was being
# drawn in the wrong part of the brain.
#
# These are approximate positions on a GENERIC asset, not a registered atlas.
# They put each site in the right territory, which is what the view needs to
# communicate; they are not a claim of anatomical precision.
#
# Laterality is NOT recorded in the dataset, so lateral sites are all placed on
# the same side rather than invented per patient.
SITE_ANCHORS: dict[str, tuple[float, float, float]] = {
    "ICA":      (-0.13,  0.16, -0.40),   # internal carotid terminus
    "MCA":      (-0.34,  0.10, -0.28),   # middle cerebral bifurcation
    "ACOM":     ( 0.00,  0.40, -0.30),   # anterior communicating, midline
    "PCOM":     (-0.17, -0.02, -0.38),   # posterior communicating
    "POST":     ( 0.00, -0.20, -0.45),   # basilar / posterior circulation
    "BASILAR":  ( 0.00, -0.20, -0.45),
}
# The PHASES score groups these into one category, and the dataset stores that
# grouped label verbatim. Anchor it at ACOM, the most common of the three.
SITE_ALIASES = {"ACOM_PCOM_POST": "ACOM"}
DEFAULT_SITE = "MCA"      # the most common aneurysm location

# How far an anchor may be pulled when snapping to real vessel geometry before
# it stops meaning what its name says. 0.05 normalised units ~ 4.6 mm.
SNAP_TOLERANCE = 0.05


def _resolve_site(site: str | None) -> str:
    key = (site or "").strip().upper()
    key = SITE_ALIASES.get(key, key)
    return key if key in SITE_ANCHORS else DEFAULT_SITE


def build(
    src_glb: Path,
    out_glb: Path,
    target_faces: int = 150_000,
) -> dict[str, Any]:
    import trimesh

    scene = trimesh.load(str(src_glb), force="scene")
    geoms = list(scene.geometry.items())
    if not geoms:
        raise RuntimeError(f"no geometry in {src_glb}")

    # Keep the largest mesh only. The viewer has always hidden the other two
    # ("solid brain-shape geometry left over from the source model" — they bury
    # the network visually), so shipping them is pure waste.
    name, mesh = max(geoms, key=lambda kv: len(kv[1].faces))
    n_faces_before = len(mesh.faces)
    n_verts_before = len(mesh.vertices)
    dropped = [n for n, _ in geoms if n != name]

    if target_faces and n_faces_before > target_faces:
        # Quadric decimation preserves the branching topology far better than
        # vertex clustering, which welds nearby but unconnected branches of a
        # vessel network into each other.
        #
        # Routed through VTK rather than trimesh.simplify_quadric_decimation,
        # which requires the optional `fast_simplification` package. PyVista is
        # already a hard dependency of this pipeline, so this adds nothing to
        # install. `volume_preservation` matters here: without it, decimation
        # thins tubular branches and the network visibly shrivels.
        import pyvista as pv

        faces = np.hstack(
            [np.full((len(mesh.faces), 1), 3), np.asarray(mesh.faces)]
        ).astype(np.int64).ravel()
        poly = pv.PolyData(np.asarray(mesh.vertices, dtype=float), faces)
        reduced = poly.decimate(
            1.0 - target_faces / n_faces_before, volume_preservation=True
        ).clean().triangulate()
        mesh = trimesh.Trimesh(
            vertices=np.asarray(reduced.points, dtype=float),
            faces=np.asarray(reduced.faces).reshape(-1, 4)[:, 1:],
            process=False,
        )

    verts = np.asarray(mesh.vertices, dtype=float)

    # Normalise: centre on the origin and scale the longest axis to 1.8 units,
    # so the viewer needs no per-asset camera tuning.
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    centre = (lo + hi) / 2.0
    extent = float((hi - lo).max()) or 1.0
    scale = 1.8 / extent
    verts = (verts - centre) * scale
    mesh.vertices = verts

    # --- bake the per-site anchors ----------------------------------------
    #
    # Each anchor is SNAPPED to real vessel geometry: the nominal anatomical
    # point is almost never exactly on a vessel in a generic asset, so the
    # local centroid of the nearest vertices is used instead. Without this a
    # bulge can inflate a region containing no geometry and render as nothing
    # at all — a silent failure, since the page still loads fine.
    from scipy.spatial import cKDTree

    units_per_mm = 1.8 / BRAIN_LENGTH_MM
    tree = cKDTree(verts)
    centroid = verts.mean(axis=0)
    sites: dict[str, Any] = {}

    for site, nominal in SITE_ANCHORS.items():
        nom = np.asarray(nominal, dtype=float)
        # 400 nearest vertices ~ a local patch of vessel, enough to average out
        # a single stray branch without dragging in the whole hemisphere.
        _, idx = tree.query(nom, k=min(400, len(verts)))
        local = verts[np.atleast_1d(idx)]
        snapped = local.mean(axis=0)

        # Outward = away from the brain's centroid, so the sac protrudes into
        # open space rather than burrowing into the middle of the network.
        out = snapped - centroid
        n = float(np.linalg.norm(out))
        out = out / n if n > 1e-9 else np.array([0.0, 0.0, -1.0])

        # A large snap means the asset carries no vessel near that anatomical
        # point, so the anchor has been dragged onto some OTHER structure and
        # no longer represents the site it is named after. Recorded rather than
        # hidden: an anchor that is quietly 20 mm off is worse than one that
        # says so. 0.05 units ~ 4.6 mm at this scale.
        snap = float(np.linalg.norm(snapped - nom))
        sites[site] = {
            "nominal": [float(v) for v in nom],
            "centre": [float(v) for v in snapped],
            "outward": [float(v) for v in out],
            "snap_distance": snap,
            "snap_distance_mm": snap / units_per_mm,
            "snap_ok": bool(snap <= SNAP_TOLERANCE),
        }

    # Retained for reference: what the original "topmost cluster in Y" rule
    # produced, so the placement change is auditable rather than silent.
    y = verts[:, 1]
    y_lo, y_hi = float(y.min()), float(y.max())
    thresh = y_lo + (y_hi - y_lo) * HOTSPOT_TOP_FRACTION
    top = verts[y >= thresh]
    hotspot = top.mean(axis=0) if len(top) else np.array([0.0, y_hi, 0.0])

    out_glb = Path(out_glb)
    out_glb.parent.mkdir(parents=True, exist_ok=True)
    out_glb.write_bytes(
        trimesh.exchange.gltf.export_glb(trimesh.Scene(mesh), include_normals=True)
    )

    meta = {
        "glb": out_glb.name,
        "bytes": out_glb.stat().st_size,
        "source": src_glb.name,
        "source_bytes": src_glb.stat().st_size,
        "kept_mesh": name,
        "dropped_meshes": dropped,
        "faces_before": int(n_faces_before),
        "faces_after": int(len(mesh.faces)),
        "vertices_before": int(n_verts_before),
        "vertices_after": int(len(verts)),
        "sites": sites,
        "site_aliases": SITE_ALIASES,
        "default_site": DEFAULT_SITE,
        "legacy_topmost_y_hotspot": [float(v) for v in hotspot],
        "units_per_mm": units_per_mm,
        "brain_length_mm": BRAIN_LENGTH_MM,
        "orientation": "+Y anterior, +Z superior, X left-right (verified by axis renders)",
        "note": (
            "Generic anatomical asset — not patient-derived. Per-patient content "
            "is the bulge: size from measured morphology, colour from computed "
            "hemodynamics."
        ),
    }
    out_glb.with_suffix(".json").write_text(json.dumps(meta, indent=2))
    return meta


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Build the compact brain asset")
    ap.add_argument("--src", default="/mnt/d/fyp/NervesOnly_v1.glb")
    ap.add_argument("--out", default="/mnt/d/fyp/models/brain.glb")
    ap.add_argument("--faces", type=int, default=150_000)
    args = ap.parse_args()

    m = build(Path(args.src), Path(args.out), args.faces)
    print(
        f"  {m['source_bytes']/1e6:.1f} MB -> {m['bytes']/1e6:.2f} MB "
        f"({m['source_bytes']/max(m['bytes'],1):.1f}x smaller), "
        f"{m['faces_before']} -> {m['faces_after']} faces"
    )
    bad = [s for s, v in m["sites"].items() if not v["snap_ok"]]
    if bad:
        print(
            f"\n  WARNING: {len(bad)} anchor(s) had no vessel geometry nearby and were "
            f"snapped onto some other structure — they no longer mark the site they name:"
        )
        for s in bad:
            print(f"    {s}: moved {m['sites'][s]['snap_distance_mm']:.1f} mm")
        print("  Cases at these sites will render the bulge in the wrong place.")
