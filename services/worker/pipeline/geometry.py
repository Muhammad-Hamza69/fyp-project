"""
Vessel surface generation and face tagging for the CFD pipeline.

Produces a watertight surface split into four NAMED regions:

    inlet · outlet · wall · wall_aneurysm

That four-way split is the single highest-leverage decision in the CFD track.
snappyHexMesh turns each named solid in the STL into an OpenFOAM patch, so
hemodynamic zone extraction later becomes a one-line patch query instead of a
topoSet dance -- and the names map 1:1 onto the frontend's ZoneId union
("inlet" | "outlet" | "neck" | "dome").

Two geometry sources share this tagging code:

  * make_sidewall_aneurysm() -- parametric cylinder + sphere. No download, fully
    reproducible, used to validate the solver chain. Real physics, synthetic
    anatomy; must be labelled as such in any report.
  * tag_patient_surface()    -- same tagging applied to a real reconstructed
    surface (AneuriskWeb-style model.stl) once patient geometry is available.

Units are METRES throughout, because OpenFOAM's incompressible solvers are
dimensionally consistent in SI and a mm-scale mesh silently produces WSS values
off by 10^3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyvista as pv

# Region names -> OpenFOAM patch names. Keep in sync with packages/shared ZoneId.
REGION_INLET = "inlet"
REGION_OUTLET = "outlet"
REGION_WALL = "wall"
REGION_WALL_ANEURYSM = "wall_aneurysm"
REGIONS = (REGION_INLET, REGION_OUTLET, REGION_WALL, REGION_WALL_ANEURYSM)


@dataclass(frozen=True)
class AneurysmGeometry:
    """Parameters for a parametric sidewall (saccular) aneurysm, in metres."""

    parent_radius: float = 0.0020        # 2.0 mm — typical MCA/ICA calibre
    parent_length: float = 0.0600        # 60 mm total
    sac_radius: float = 0.0040           # 4.0 mm dome
    neck_offset: float = 0.0014          # sac centre lift above the wall
    # Inlet extension so a plug profile develops before reaching the sac.
    # 10 diameters is the usual rule of thumb; with r=2mm that is 40mm.
    inlet_extension: float = 0.0400
    resolution: int = 120                # circumferential subdivisions

    @property
    def sac_centre_x(self) -> float:
        """Sac sits downstream of the inlet extension."""
        return self.inlet_extension + 0.0080

    @property
    def sac_centre_y(self) -> float:
        return self.parent_radius + self.neck_offset

    @property
    def total_length(self) -> float:
        return self.inlet_extension + self.parent_length


def _classify_faces(
    surf: pv.PolyData,
    geom: AneurysmGeometry,
    axis_tol: float = 1e-5,
) -> np.ndarray:
    """
    Assign every triangle to one of the four regions using cell centroids.

    Ordering matters: inlet/outlet caps are claimed first (they are planar and
    unambiguous), then the aneurysm sac, then everything remaining is parent
    artery wall. Doing it the other way round would let the sac steal cap cells.
    """
    centres = surf.cell_centers().points
    x, y, z = centres[:, 0], centres[:, 1], centres[:, 2]

    labels = np.full(surf.n_cells, REGION_WALL, dtype=object)

    x_min, x_max = x.min(), x.max()
    labels[x < x_min + axis_tol] = REGION_INLET
    labels[x > x_max - axis_tol] = REGION_OUTLET

    # Sac membership: inside the dome sphere (with a small margin) AND clear of
    # the parent-artery centreline, so the neck region is attributed to the sac
    # rather than smeared into the parent wall.
    d_sac = np.sqrt(
        (x - geom.sac_centre_x) ** 2
        + (y - geom.sac_centre_y) ** 2
        + z**2
    )
    is_sac = (d_sac <= geom.sac_radius * 1.02) & (y > geom.parent_radius * 0.55)
    still_wall = labels == REGION_WALL
    labels[is_sac & still_wall] = REGION_WALL_ANEURYSM

    return labels


def _triangles(surf: pv.PolyData) -> np.ndarray:
    """
    Return an (n_cells, 3) point-index array, verifying the mesh really is
    all-triangles first.

    `surf.faces` is a flat VTK connectivity array [n, i0..in-1, n, ...]. Blindly
    reshaping it to (-1, 4) silently misaligns if any cell is not a triangle,
    which surfaces later as an out-of-range vertex index rather than an obvious
    error -- so check the size stride explicitly.
    """
    faces = np.asarray(surf.faces)
    if faces.size % 4 != 0:
        raise ValueError(f"non-triangular mesh: faces array size {faces.size}")
    quads = faces.reshape(-1, 4)
    if not np.all(quads[:, 0] == 3):
        raise ValueError("mesh contains non-triangular cells; triangulate first")
    if quads.shape[0] != surf.n_cells:
        raise ValueError(
            f"cell count mismatch: {quads.shape[0]} vs {surf.n_cells}"
        )
    return quads[:, 1:]


def _write_multisolid_stl(
    surf: pv.PolyData, labels: np.ndarray, out_path: Path
) -> dict[str, int]:
    """
    Write one ASCII STL containing a separate `solid <name>` block per region.

    snappyHexMesh reads this single file and exposes each solid as a selectable
    region, which is what lets us name patches without any topoSet work.
    PyVista cannot write multi-solid STL, so emit it directly -- the format is
    trivial and being explicit avoids a silent single-solid collapse.
    """
    points = surf.points
    faces = _triangles(surf)
    counts: dict[str, int] = {}

    with out_path.open("w") as fh:
        for region in REGIONS:
            idx = np.where(labels == region)[0]
            counts[region] = len(idx)
            fh.write(f"solid {region}\n")
            for ci in idx:
                a, b, c = points[faces[ci]]
                n = np.cross(b - a, c - a)
                norm = np.linalg.norm(n)
                n = n / norm if norm > 0 else np.array([0.0, 0.0, 1.0])
                fh.write(f"  facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}\n")
                fh.write("    outer loop\n")
                for v in (a, b, c):
                    fh.write(f"      vertex {v[0]:.6e} {v[1]:.6e} {v[2]:.6e}\n")
                fh.write("    endloop\n  endfacet\n")
            fh.write(f"endsolid {region}\n")

    return counts


def _sdf_capped_cylinder(
    x: np.ndarray, y: np.ndarray, z: np.ndarray,
    x0: float, x1: float, radius: float,
) -> np.ndarray:
    """Signed distance to a finite, capped cylinder aligned with +X."""
    half = (x1 - x0) / 2.0
    cx = (x0 + x1) / 2.0
    d_axial = np.abs(x - cx) - half
    d_radial = np.sqrt(y * y + z * z) - radius
    outside = np.sqrt(
        np.maximum(d_axial, 0.0) ** 2 + np.maximum(d_radial, 0.0) ** 2
    )
    inside = np.minimum(np.maximum(d_axial, d_radial), 0.0)
    return outside + inside


def make_sidewall_aneurysm(
    geom: AneurysmGeometry | None = None,
    spacing: float = 1.5e-4,
) -> tuple[pv.PolyData, np.ndarray]:
    """
    Build the parametric surface via a signed-distance field + marching cubes.

    Why not a CSG boolean: VTK's boolean_union on a cylinder/sphere intersection
    produced 11,101 unpaired edges (surfaceCheck: "surface is not closed") plus
    ~13% sliver triangles down to quality 2.6e-9. snappyHexMesh needs a closed
    surface to resolve inside/outside, so that route is a dead end.

    An SDF union is min(d_a, d_b) and marching cubes on it is closed and
    manifold by construction, with uniform well-shaped triangles. It also
    mirrors the real pipeline -- patient geometry arrives as a segmentation mask
    that goes through exactly this marching-cubes step -- so the downstream code
    is exercised the same way for synthetic and real input.

    `spacing` is the isotropic voxel size in metres; 1.5e-4 (0.15 mm) puts ~80
    triangles around a 2 mm-radius vessel, which is ample for snappyHexMesh
    since it only ray-casts against this surface.
    """
    geom = geom or AneurysmGeometry()

    pad = 4.0 * spacing
    x_lo, x_hi = -pad, geom.total_length + pad
    y_lo = -geom.parent_radius - pad
    y_hi = geom.sac_centre_y + geom.sac_radius + pad
    z_lim = geom.sac_radius + pad

    nx = int(np.ceil((x_hi - x_lo) / spacing)) + 1
    ny = int(np.ceil((y_hi - y_lo) / spacing)) + 1
    nz = int(np.ceil((2 * z_lim) / spacing)) + 1

    grid = pv.ImageData(
        dimensions=(nx, ny, nz),
        spacing=(spacing, spacing, spacing),
        origin=(x_lo, y_lo, -z_lim),
    )
    pts = grid.points
    gx, gy, gz = pts[:, 0], pts[:, 1], pts[:, 2]

    d_parent = _sdf_capped_cylinder(
        gx, gy, gz, 0.0, geom.total_length, geom.parent_radius
    )
    d_sac = (
        np.sqrt(
            (gx - geom.sac_centre_x) ** 2
            + (gy - geom.sac_centre_y) ** 2
            + gz**2
        )
        - geom.sac_radius
    )
    grid["sdf"] = np.minimum(d_parent, d_sac)

    surf = grid.contour(isosurfaces=[0.0], scalars="sdf")
    surf = surf.triangulate().clean()

    # clean() can leave stray vertex/line cells behind. Those count towards
    # n_cells but never appear in the faces array, so every per-cell array
    # (labels, areas, centroids) silently shifts relative to the triangles.
    # Rebuilding from points+faces drops them and keeps the indexing honest.
    surf = pv.PolyData(surf.points, faces=surf.faces).clean()

    surf = surf.compute_normals(
        consistent_normals=True, auto_orient_normals=True, inplace=False
    )

    labels = _classify_faces(surf, geom, axis_tol=spacing * 1.5)
    return surf, labels


def write_surface(
    surf: pv.PolyData,
    labels: np.ndarray,
    out_dir: Path,
    name: str = "vessel",
) -> dict[str, object]:
    """Write the tagged surface as multi-solid STL plus a per-region summary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stl_path = out_dir / f"{name}.stl"
    counts = _write_multisolid_stl(surf, labels, stl_path)

    areas = surf.compute_cell_sizes(length=False, area=True, volume=False)
    cell_area = np.asarray(areas["Area"])
    region_area = {r: float(cell_area[labels == r].sum()) for r in REGIONS}

    return {
        "stl": str(stl_path),
        "n_points": int(surf.n_points),
        "n_cells": int(surf.n_cells),
        "is_manifold": bool(surf.is_manifold),
        "counts": counts,
        "area_m2": region_area,
        "bounds": [float(b) for b in surf.bounds],
    }


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Generate a tagged vessel surface")
    ap.add_argument("--out", default="~/cases/synthetic01/constant/triSurface")
    ap.add_argument("--name", default="vessel")
    args = ap.parse_args()

    geometry = AneurysmGeometry()
    surface, region_labels = make_sidewall_aneurysm(geometry)
    summary = write_surface(
        surface, region_labels, Path(args.out).expanduser(), args.name
    )
    print(json.dumps(summary, indent=2))
