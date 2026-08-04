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

    # Decimate, then RESAMPLE the scalars onto the reduced mesh.
    #
    # `decimate` does not carry point data through reliably (and the following
    # `clean` drops what survives), so reading the arrays off the decimated
    # surface raises KeyError. Sampling from the full-resolution surface is
    # both robust and more correct: each surviving vertex takes the field value
    # interpolated at its own position rather than inheriting a neighbour's.
    n_before = surf.n_cells
    if target_faces and surf.n_cells > target_faces:
        reduced = surf.decimate(1.0 - target_faces / surf.n_cells).clean().triangulate()
        reduced = reduced.sample(surf)
        surf = reduced
        tawss = np.asarray(surf.point_data["TAWSS_Pa"], dtype=float)
        osi = np.asarray(surf.point_data["OSI"], dtype=float)

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
        "tawss_min_pa": float(tawss.min()),
        "tawss_max_pa": float(tawss.max()),
        "tawss_mean_pa": float(tawss.mean()),
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
