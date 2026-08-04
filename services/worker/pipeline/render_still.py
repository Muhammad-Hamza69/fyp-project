"""
Off-screen renders of the solved vessel surface, for documentation and figures.

These are pictures of the actual OpenFOAM wall patches, coloured by the
computed TAWSS/OSI field using the same normalisation as export_mesh.py and the
2D heatmap.

NOT THE BROWSER FALLBACK ANY MORE. The dashboard's 3D panel now shows the
cerebral vasculature with a per-case aneurysm sac, so the WebGL fallback has to
be a picture of THAT — it is produced by render_brain.py and lives at
models/{id}.png. Output here goes to models/surface/ instead, because writing
next to the GLBs would land on exactly those filenames and quietly swap the
fallback for a picture of something else, with the page none the wiser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pyvista as pv

STABLE_RGB = np.array([0x1F, 0x5F, 0x99], dtype=float)
CRITICAL_RGB = np.array([0xB8, 0x32, 0x32], dtype=float)
TAWSS_MIN, TAWSS_MAX = 0.15, 1.5
OSI_MIN, OSI_MAX = 0.03, 0.35
RHO = 1060.0
BG = "#0b1524"


def _risk_factor(tawss_pa: np.ndarray, osi: np.ndarray, mode: str) -> np.ndarray:
    if mode.upper() == "OSI":
        f = (osi - OSI_MIN) / (OSI_MAX - OSI_MIN)
    else:
        f = 1.0 - (tawss_pa - TAWSS_MIN) / (TAWSS_MAX - TAWSS_MIN)
    return np.clip(f, 0.0, 1.0)


def render(glb_path: Path, out_png: Path, mode: str = "TAWSS",
           size: tuple[int, int] = (1200, 900)) -> dict[str, Any]:
    """
    Render a GLB to PNG off-screen.

    Reads the colours already baked into the GLB rather than recomputing them,
    which guarantees the still and the interactive view are the same picture.
    """
    import trimesh

    scene = trimesh.load(str(glb_path), force="scene")
    geoms = list(scene.geometry.values())
    if not geoms:
        raise RuntimeError(f"no geometry in {glb_path}")
    tm = geoms[0]

    faces = np.hstack([np.full((len(tm.faces), 1), 3), tm.faces]).astype(np.int64).ravel()
    surf = pv.PolyData(np.asarray(tm.vertices, dtype=float), faces)

    colours = None
    vc = getattr(tm.visual, "vertex_colors", None)
    if vc is not None and len(vc) == len(tm.vertices):
        colours = np.asarray(vc)[:, :3].astype(np.uint8)
        surf.point_data["RGB"] = colours

    pv.OFF_SCREEN = True
    pl = pv.Plotter(off_screen=True, window_size=list(size))
    pl.set_background(BG)
    if colours is not None:
        pl.add_mesh(surf, scalars="RGB", rgb=True, smooth_shading=True,
                    specular=0.15, specular_power=12)
    else:
        pl.add_mesh(surf, color="#1F5F99", smooth_shading=True)

    # Three-quarter view: shows the sac protruding from the parent artery
    # rather than hiding it behind the vessel, which a pure side view does.
    pl.camera_position = "yz"
    pl.camera.azimuth = 35
    pl.camera.elevation = 20
    pl.reset_camera()
    # The vessel is a long thin cylinder, so reset_camera frames it to its
    # LENGTH and leaves the sac — the only part anyone is looking at — small.
    # Zoom harder and accept clipping the far ends of the parent artery.
    pl.camera.zoom(2.1)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    pl.screenshot(str(out_png), transparent_background=False)
    pl.close()

    return {
        "png": str(out_png),
        "bytes": out_png.stat().st_size,
        "mode": mode,
        "vertices": int(len(tm.vertices)),
        "faces": int(len(tm.faces)),
        "had_vertex_colours": colours is not None,
    }


def render_all(models_dir: Path, out_dir: Path | None = None) -> list[dict[str, Any]]:
    """
    Render every solved surface in `models_dir` to PNG.

    Output goes to `models/surface/`, NOT alongside the GLBs.
    `models/{id}.png` is the browser's WebGL fallback for the brain view, and
    writing there would silently replace those with pictures of the bare vessel
    surface — a different subject entirely, under filenames the viewer trusts.
    The page would keep working and show the wrong thing.

    `brain.glb` is skipped: it is a generic anatomical asset with no solved
    field on it, so there is nothing here to colour it by.
    """
    models_dir = Path(models_dir)
    out_dir = Path(out_dir) if out_dir else models_dir / "surface"
    out_dir.mkdir(parents=True, exist_ok=True)

    out = []
    for glb in sorted(models_dir.glob("*.glb")):
        if glb.stem == "brain":
            continue
        mode = "OSI" if glb.stem.endswith("-osi") else "TAWSS"
        png = out_dir / f"{glb.stem}.png"
        try:
            out.append(render(glb, png, mode))
        except Exception as exc:  # noqa: BLE001
            out.append({"png": str(png), "error": f"{exc.__class__.__name__}: {exc}"})
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Render solved surfaces to PNG")
    ap.add_argument("--models", default="/mnt/d/fyp/models")
    ap.add_argument("--out", default=None,
                    help="output directory (default models/surface — NOT models/, "
                         "which holds the browser's brain-view fallbacks)")
    args = ap.parse_args()
    print(json.dumps(render_all(Path(args.models),
                                Path(args.out) if args.out else None), indent=2))
