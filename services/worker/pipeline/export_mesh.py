"""
Export the solved vessel surface as a compact, colour-baked GLB for the browser.

REPLACES `NervesOnly_v1.glb` — a 35.7 MB asset that took 70 s to download and,
more importantly, was a generic nerve model with no relationship to any
patient's vasculature. The viewer coloured it by approximating the "aneurysm"
as the topmost cluster of vertices.

What ships instead is the actual surface the solver ran on, with per-vertex
colour baked from the computed TAWSS field. The 3D view stops being an
illustration and becomes a rendering of the result.

SIZE
----
Three levers, in order of effect:
  1. decimation to a triangle budget (the wall mesh is ~47k faces; the browser
     needs far fewer to look correct at screen resolution)
  2. Draco compression, which Three.js decodes natively
  3. baking colour into vertices rather than shipping a scalar field plus a
     colourmap, so no shader work is needed client-side

Typical output is 1–3 MB against 35.7 MB — roughly a 20x reduction, and it is
patient-specific rather than generic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pyvista as pv

# Same endpoints as the 2D heatmap and the risk library, so the 3D view cannot
# disagree with the gauges beside it.
STABLE_RGB = np.array([0x1F, 0x5F, 0x99], dtype=float)
CRITICAL_RGB = np.array([0xB8, 0x32, 0x32], dtype=float)

TAWSS_MIN, TAWSS_MAX = 0.15, 1.5     # normalisation range, Pa
OSI_MIN, OSI_MAX = 0.03, 0.35

RHO = 1060.0


def _risk_factor(tawss_pa: np.ndarray, osi: np.ndarray, mode: str) -> np.ndarray:
    """0..1 risk, matching packages/shared/src/risk.ts `riskFactor` exactly."""
    if mode.upper() == "OSI":
        f = (osi - OSI_MIN) / (OSI_MAX - OSI_MIN)
    else:
        # TAWSS is inverted: LOW shear is the risk factor.
        f = 1.0 - (tawss_pa - TAWSS_MIN) / (TAWSS_MAX - TAWSS_MIN)
    return np.clip(f, 0.0, 1.0)


def _colours(factor: np.ndarray) -> np.ndarray:
    """Linear blue → red interpolation, returned as uint8 RGBA."""
    rgb = STABLE_RGB[None, :] + (CRITICAL_RGB - STABLE_RGB)[None, :] * factor[:, None]
    rgba = np.concatenate([rgb, np.full((len(factor), 1), 255.0)], axis=1)
    return np.clip(rgba, 0, 255).astype(np.uint8)


def build(
    case_dir: Path,
    out_glb: Path,
    mode: str = "TAWSS",
    target_faces: int = 40000,
) -> dict[str, Any]:
    case_dir = Path(case_dir).expanduser()
    foam = case_dir / "case.foam"
    foam.touch(exist_ok=True)

    reader = pv.OpenFOAMReader(str(foam))
    reader.enable_all_patch_arrays()
    times = list(reader.time_values)
    if not times:
        raise RuntimeError(f"no time directories in {case_dir}")
    reader.set_active_time_value(times[-1])
    mesh = reader.read()

    parts: list[pv.PolyData] = []

    def walk(block: Any) -> None:
        if isinstance(block, pv.MultiBlock):
            for i in range(block.n_blocks):
                name = block.get_block_name(i) or ""
                child = block[i]
                if isinstance(child, pv.MultiBlock):
                    walk(child)
                elif child is not None and name in ("wall", "wall_aneurysm"):
                    parts.append(child.extract_surface())

    walk(mesh)
    if not parts:
        raise RuntimeError("no wall patches found")

    surf = parts[0].copy()
    for extra in parts[1:]:
        surf = surf.merge(extra)
    surf = surf.triangulate().clean()

    # Wall shear stress -> TAWSS in Pa.  OpenFOAM is kinematic: x rho.
    name = ("wallShearStressMean" if "wallShearStressMean" in surf.array_names
            else "wallShearStress")
    if name in surf.cell_data:
        surf = surf.cell_data_to_point_data()
    tau = np.asarray(surf.point_data[name], dtype=float)
    tawss = np.linalg.norm(tau, axis=1) * RHO

    magname = "magWallShearStressMean"
    if magname in surf.point_data:
        mag = np.abs(np.asarray(surf.point_data[magname], dtype=float))
        with np.errstate(divide="ignore", invalid="ignore"):
            osi = 0.5 * (1.0 - np.where(mag > 1e-30,
                                        np.linalg.norm(tau, axis=1) / mag, 1.0))
        osi = np.clip(np.nan_to_num(osi), 0.0, 0.5)
        tawss = mag * RHO
    else:
        osi = np.zeros_like(tawss)

    surf.point_data["TAWSS_Pa"] = tawss
    surf.point_data["OSI"] = osi

    # Field statistics are taken HERE, at full resolution and area-weighted,
    # because neither property survives decimation honestly:
    #
    #  - Decimation is curvature-driven. It preserves vertices on the highly
    #    curved sac (low shear) and collapses the smooth parent artery (high
    #    shear), so a vertex mean over the decimated mesh is biased low —
    #    measured at 0.31 Pa against a true 1.30 Pa on PT-2026-0102.
    #  - A vertex mean is not area-weighted in any case. Mesh refinement
    #    clusters vertices where cells are small, which silently weights those
    #    regions more heavily than the area they actually occupy.
    #
    # Area-weighting over the original surface gives the same quantity the
    # hemodynamic engine reports, so the sidecar and the engine agree.
    _cs = surf.point_data_to_cell_data().compute_cell_sizes(
        length=False, area=True, volume=False)
    _area = np.asarray(_cs.cell_data["Area"], dtype=float)
    _tot = float(_area.sum())
    _tawss_cells = np.asarray(_cs.cell_data["TAWSS_Pa"], dtype=float)
    stats = {
        "tawss_min_pa": float(tawss.min()),
        "tawss_max_pa": float(tawss.max()),
        "tawss_mean_pa": float((_tawss_cells * _area).sum() / _tot) if _tot else 0.0,
        "tawss_mean_basis": "area-weighted over full-resolution wall patches",
        "wall_area_mm2": _tot * 1e6,
    }

    # Decimate, then transfer the scalars by NEAREST NEIGHBOUR.
    #
    # `decimate` does not carry point data through reliably (and the following
    # `clean` drops what survives), so the values must be transferred back.
    #
    # PolyData.sample() was tried and is WRONG here. It is a probe filter: it
    # requires each target point to fall geometrically INSIDE a source cell.
    # For a 2-D surface embedded in 3-D, decimated vertices lie on the surface
    # but miss cell containment by floating-point margins, and the probe then
    # returns 0.0 for them. Measured on PT-2026-0102: 15,550 of 20,091
    # vertices (77.4%) came back as exactly zero, with vtkValidPointMask == 0
    # for precisely those points. Zero TAWSS maps to maximum risk, so 77% of
    # the mesh rendered solid red — a completely wrong picture, produced
    # without a single error.
    #
    # A KD-tree lookup has no containment requirement. Every decimated vertex
    # is a subset of (or extremely close to) an original vertex, so nearest
    # neighbour is not an approximation here — it recovers the exact value.
    n_before = surf.n_cells
    if target_faces and surf.n_cells > target_faces:
        from scipy.spatial import cKDTree

        src_pts = np.asarray(surf.points, dtype=float)
        reduced = surf.decimate(1.0 - target_faces / surf.n_cells).clean().triangulate()
        _, idx = cKDTree(src_pts).query(np.asarray(reduced.points, dtype=float), k=1)

        tawss = np.asarray(tawss, dtype=float)[idx]
        osi = np.asarray(osi, dtype=float)[idx]
        reduced.point_data["TAWSS_Pa"] = tawss
        reduced.point_data["OSI"] = osi
        surf = reduced

        # A zero here would mean the transfer failed again; the source field
        # has no exact zeros, so this cannot legitimately trigger.
        n_zero = int((tawss == 0.0).sum())
        if n_zero:
            raise RuntimeError(
                f"{n_zero} vertices have TAWSS exactly 0 after transfer — "
                "scalar transfer failed"
            )

    colours = _colours(_risk_factor(tawss, osi, mode))

    # Centre and scale to unit-ish size so the viewer needs no per-case camera
    # tuning; the physical scale is reported separately in the sidecar.
    pts = np.asarray(surf.points, dtype=float)
    centre = pts.mean(axis=0)
    pts = pts - centre
    extent = float(np.abs(pts).max()) or 1.0
    pts = pts / extent

    faces = np.asarray(surf.faces).reshape(-1, 4)[:, 1:]

    import trimesh

    tm = trimesh.Trimesh(vertices=pts, faces=faces, process=False)
    tm.visual = trimesh.visual.ColorVisuals(mesh=tm, vertex_colors=colours)

    out_glb = Path(out_glb)
    out_glb.parent.mkdir(parents=True, exist_ok=True)
    out_glb.write_bytes(trimesh.exchange.gltf.export_glb(
        trimesh.Scene(tm), include_normals=True))

    meta = {
        "glb": str(out_glb),
        "bytes": out_glb.stat().st_size,
        "mode": mode,
        "faces_before": int(n_before),
        "faces_after": int(len(faces)),
        "vertices": int(len(pts)),
        **stats,
        # Range actually baked into the vertex colours. It should bracket the
        # full-res range above; a collapse to near-zero would mean the scalar
        # transfer regressed.
        "baked_min_pa": float(tawss.min()),
        "baked_max_pa": float(tawss.max()),
        "scale_m_per_unit": extent,
        "centre_m": [float(c) for c in centre],
    }
    out_glb.with_suffix(".json").write_text(json.dumps(meta, indent=2))
    return meta


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Export solved surface as a coloured GLB")
    ap.add_argument("case")
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", default="TAWSS", choices=["TAWSS", "OSI"])
    ap.add_argument("--faces", type=int, default=40000)
    args = ap.parse_args()

    print(json.dumps(build(Path(args.case), Path(args.out), args.mode, args.faces), indent=2))
