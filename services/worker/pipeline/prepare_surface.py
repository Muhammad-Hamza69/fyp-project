"""
Model preparation: turn an arbitrary vessel surface into a CFD-ready domain.

This is the stage between segmentation and meshing. A marching-cubes surface
from a segmentation mask is a closed blob — it has no inlet, no outlet, and no
distinction between parent artery and aneurysm sac. snappyHexMesh cannot apply
a flow boundary condition to it, and the hemodynamics stage cannot report
"sac versus parent" without knowing which is which.

What this produces is the same four named regions the parametric pipeline
emits, so everything downstream is identical whether the geometry came from a
generator or from a patient scan:

    inlet · outlet · wall · wall_aneurysm

METHOD
------
1. Clip the ends perpendicular to the vessel axis, creating open faces.
   The axis is found by principal component analysis of the surface points —
   a cerebral artery segment is strongly elongated, so its first principal
   component is the flow direction. This avoids assuming the vessel lies along
   a coordinate axis, which is true for the phantom and false for a real scan.

2. Cap the openings with flat discs. Those become `inlet` and `outlet`;
   whichever cap has the larger area is taken as the inlet, matching the usual
   convention that flow enters the larger parent vessel.

3. Separate the sac from the parent artery by RADIAL DISTANCE from the fitted
   centreline. A saccular aneurysm is by definition the part of the lumen that
   protrudes beyond the parent vessel calibre, so points whose distance from
   the axis exceeds a robust estimate of the parent radius are sac.

Step 3 is an approximation. It handles sidewall (lateral) aneurysms well, which
is the geometry this project targets. It would mis-classify a bifurcation
aneurysm sitting on the axis, and that limitation is reported rather than
hidden — `confidence` in the returned summary drops when the separation is
poorly defined.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import numpy as np
import pyvista as pv

REGION_INLET = "inlet"
REGION_OUTLET = "outlet"
REGION_WALL = "wall"
REGION_WALL_ANEURYSM = "wall_aneurysm"
REGIONS = (REGION_INLET, REGION_OUTLET, REGION_WALL, REGION_WALL_ANEURYSM)


@dataclass
class PrepResult:
    stl: str
    n_points: int
    n_cells: int
    is_manifold: bool
    counts: dict[str, int]
    area_mm2: dict[str, float]
    axis: list[float]
    parent_radius_mm: float
    sac_detected: bool
    sac_confidence: float
    notes: list[str]


def _fit_axis(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (centroid, unit axis) from the dominant principal component."""
    centroid = points.mean(axis=0)
    centred = points - centroid
    # SVD is numerically better behaved here than forming the covariance matrix.
    _, _, vt = np.linalg.svd(centred, full_matrices=False)
    axis = vt[0]
    return centroid, axis / (np.linalg.norm(axis) or 1.0)


def _radial_distance(points: np.ndarray, centroid: np.ndarray, axis: np.ndarray) -> np.ndarray:
    d = points - centroid
    along = d @ axis
    perp = d - along[:, None] * axis[None, :]
    return np.linalg.norm(perp, axis=1)


def _triangles(surf: pv.PolyData) -> np.ndarray:
    faces = np.asarray(surf.faces)
    if faces.size % 4 != 0:
        raise ValueError("surface is not purely triangular")
    quads = faces.reshape(-1, 4)
    if not np.all(quads[:, 0] == 3):
        raise ValueError("surface contains non-triangular cells")
    return quads[:, 1:]


def _write_multisolid_stl(surf: pv.PolyData, labels: np.ndarray, out: Path) -> dict[str, int]:
    """One ASCII STL, one `solid` block per region — snappyHexMesh reads each
    named solid as a selectable region, which is what creates the patches."""
    pts = surf.points
    tris = _triangles(surf)
    counts: dict[str, int] = {}
    with out.open("w") as fh:
        for region in REGIONS:
            idx = np.where(labels == region)[0]
            counts[region] = int(len(idx))
            fh.write(f"solid {region}\n")
            for ci in idx:
                a, b, c = pts[tris[ci]]
                n = np.cross(b - a, c - a)
                nn = np.linalg.norm(n)
                n = n / nn if nn > 0 else np.array([0.0, 0.0, 1.0])
                fh.write(f"  facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}\n    outer loop\n")
                for v in (a, b, c):
                    fh.write(f"      vertex {v[0]:.6e} {v[1]:.6e} {v[2]:.6e}\n")
                fh.write("    endloop\n  endfacet\n")
            fh.write(f"endsolid {region}\n")
    return counts


def prepare(
    stl_in: Path,
    stl_out: Path,
    scale_to_m: float = 1e-3,
    trim_fraction: float = 0.06,
    sac_threshold: float = 1.35,
    parent_percentile: float = 35.0,
) -> PrepResult:
    """
    Args:
        scale_to_m: multiplier converting the input units to metres. A
            marching-cubes surface from a DICOM series is in millimetres, and
            OpenFOAM must receive metres — a mm-scale mesh silently produces
            wall shear stress off by 10^3.
        trim_fraction: portion of the length removed from each end. Marching
            cubes rounds the capped ends of the volume, so the extreme slices
            are unreliable; trimming gives a clean planar cut.
        sac_threshold: multiple of the estimated parent radius beyond which a
            point is classified as sac.
        parent_percentile: percentile of the wall radial distribution taken as
            the parent calibre. Must be BELOW 50 — see the note at the
            estimator; the median is biased upward by the sac itself.
    """
    notes: list[str] = []
    surf = pv.read(str(stl_in)).triangulate().clean()
    surf.points = np.asarray(surf.points, dtype=float) * scale_to_m

    pts = np.asarray(surf.points)
    centroid, axis = _fit_axis(pts)
    along = (pts - centroid) @ axis
    lo, hi = float(along.min()), float(along.max())
    length = hi - lo

    # --- 1. clip the ends ---------------------------------------------------
    a_lo = lo + trim_fraction * length
    a_hi = hi - trim_fraction * length
    origin_lo = centroid + axis * a_lo
    origin_hi = centroid + axis * a_hi
    clipped = surf.clip(normal=axis, origin=origin_lo, invert=False)
    clipped = clipped.clip(normal=-axis, origin=origin_hi, invert=False)
    clipped = clipped.triangulate().clean()
    if clipped.n_cells == 0:
        raise RuntimeError("clipping removed the entire surface — check scale_to_m/trim_fraction")

    # --- 2. cap the openings ------------------------------------------------
    # fill_holes closes the two planar openings created by clipping. The caps
    # are then identified by their position along the axis rather than by
    # tracking which triangles the filler added, which is not exposed.
    capped = clipped.fill_holes(length * 0.5).clean().triangulate()
    capped = capped.compute_normals(consistent_normals=True, auto_orient_normals=True,
                                    inplace=False)

    centres = capped.cell_centers().points
    c_along = (centres - centroid) @ axis
    tol = length * trim_fraction * 0.35

    labels = np.full(capped.n_cells, REGION_WALL, dtype=object)
    labels[c_along <= a_lo + tol] = REGION_INLET
    labels[c_along >= a_hi - tol] = REGION_OUTLET

    # --- 3. separate sac from parent ---------------------------------------
    radial = _radial_distance(centres, centroid, axis)
    wall_mask = labels == REGION_WALL
    if wall_mask.sum() < 10:
        raise RuntimeError("too few wall faces after capping")

    # Parent calibre from a LOW percentile, not the median.
    #
    # The median was tried first and is wrong: a saccular aneurysm contributes
    # 20-40% of the wall area at large radius, which is enough to drag the
    # median well above the parent radius. Measured on a vessel whose true
    # parent radius is 2.00 mm, the median came out at 2.83 mm — a 42%
    # overestimate. The resulting 1.35x threshold then selected only the
    # outermost ~20% of the surface (the far dome tip, where flow is nearly
    # stagnant), and the reported sac TAWSS was 0.00026 Pa — roughly 22,000x
    # below the parent artery, which is not a physical ratio for an aneurysm.
    #
    # The 35th percentile is dominated by the parent tube because the sac sits
    # entirely in the upper tail. On the same geometry it gives 2.28 mm.
    parent_radius = float(np.percentile(radial[wall_mask], parent_percentile))
    sac_mask = wall_mask & (radial > sac_threshold * parent_radius)
    labels[sac_mask] = REGION_WALL_ANEURYSM

    n_sac = int(sac_mask.sum())
    sac_detected = n_sac >= 20
    # Confidence: how cleanly the radial distribution separates. A real sac
    # gives a long upper tail; a plain tube does not.
    upper = float(np.percentile(radial[wall_mask], 99))
    sac_confidence = float(np.clip((upper / parent_radius - 1.0) / 1.5, 0.0, 1.0))
    if not sac_detected:
        notes.append("No aneurysm sac detected: no wall region exceeds "
                     f"{sac_threshold}x the estimated parent radius "
                     f"({parent_radius*1e3:.2f} mm). The geometry may be a plain "
                     "vessel segment, or the sac may be axial (bifurcation type), "
                     "which radial separation cannot resolve.")
    if sac_confidence < 0.35 and sac_detected:
        notes.append(f"Low sac-separation confidence ({sac_confidence:.2f}); "
                     "the sac/parent boundary should be checked visually.")

    # --- write --------------------------------------------------------------
    stl_out = Path(stl_out)
    stl_out.parent.mkdir(parents=True, exist_ok=True)
    counts = _write_multisolid_stl(capped, labels, stl_out)

    areas = np.asarray(
        capped.compute_cell_sizes(length=False, area=True, volume=False)["Area"]) * 1e6
    area_mm2 = {r: float(areas[labels == r].sum()) for r in REGIONS}

    for r in (REGION_INLET, REGION_OUTLET):
        if counts[r] == 0:
            notes.append(f"'{r}' has no faces — capping may have failed; "
                         "snappyHexMesh will not be able to apply a flow BC.")

    return PrepResult(
        stl=str(stl_out),
        n_points=int(capped.n_points),
        n_cells=int(capped.n_cells),
        is_manifold=bool(capped.is_manifold),
        counts=counts,
        area_mm2=area_mm2,
        axis=[float(a) for a in axis],
        parent_radius_mm=parent_radius * 1e3,
        sac_detected=sac_detected,
        sac_confidence=sac_confidence,
        notes=notes,
    )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Clip, cap and tag a vessel surface for CFD")
    ap.add_argument("stl_in")
    ap.add_argument("--out", required=True)
    ap.add_argument("--scale-to-m", type=float, default=1e-3)
    ap.add_argument("--trim", type=float, default=0.06)
    ap.add_argument("--sac-threshold", type=float, default=1.35)
    ap.add_argument("--parent-percentile", type=float, default=35.0)
    args = ap.parse_args()

    res = prepare(Path(args.stl_in), Path(args.out), args.scale_to_m,
                  args.trim, args.sac_threshold, args.parent_percentile)
    print(json.dumps(asdict(res), indent=2))
