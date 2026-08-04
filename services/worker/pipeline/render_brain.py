"""
Server-side renders of the brain view, one per case.

Two jobs:

  1. The WebGL fallback. A machine with hardware acceleration disabled cannot
     create a WebGL context, so the interactive viewer cannot run at all. These
     images let that machine still SEE the case rather than an error box.

  2. Verification. The bulge maths lives in neuro3d.js and runs in a browser,
     where it is awkward to inspect. This mirrors it exactly, so the geometry
     can be checked by looking at it.

The mirroring is deliberate and load-bearing: if these two drift apart, the
fallback stops showing what the interactive view shows. The constants and the
displacement formula below must stay in step with `applyBulge` in neuro3d.js.
The same discipline already applies to risk.ts / composite.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

# --- must match neuro3d.js -------------------------------------------------
STABLE_RGB = np.array([0x1F, 0x5F, 0x99], dtype=float)
CRITICAL_RGB = np.array([0xB8, 0x32, 0x32], dtype=float)
TAWSS_MIN, TAWSS_MAX = 0.15, 1.5
OSI_MIN, OSI_MAX = 0.03, 0.35
# Fixed viewing distance in model units, the same for every case so sac sizes
# are directly comparable between patients. At a 45 deg field of view this
# frames roughly 45 mm across — an 11 mm sac with enough surrounding vessel to
# place it.
VIEW_DISTANCE = 0.52
# ---------------------------------------------------------------------------

BG = "#0b1524"


def _risk_factor(dome: dict[str, Any], mode: str) -> float:
    if mode.upper() == "OSI":
        f = (float(dome.get("osi", 0.0)) - OSI_MIN) / (OSI_MAX - OSI_MIN)
    else:
        f = 1.0 - (float(dome.get("tawss", 0.0)) - TAWSS_MIN) / (TAWSS_MAX - TAWSS_MIN)
    return float(np.clip(f, 0.0, 1.0))


def _resolve_site(meta: dict[str, Any], patient: dict[str, Any]) -> str:
    raw = str(patient.get("demographics", {}).get("site", "")).strip().upper()
    key = meta.get("site_aliases", {}).get(raw, raw)
    return key if key in meta["sites"] else meta["default_site"]


def sac_params(meta: dict[str, Any], patient: dict[str, Any], mode: str) -> dict[str, Any]:
    """
    Position, dimensions and colour of this case's aneurysm sac.

    Mirrors buildSac() in neuro3d.js. Every dimension is measured, not chosen:
    width = maxDiameter, neck = neckDiameterMm, height = aspectRatio x neck
    (aspect ratio is defined as dome height over neck width).
    """
    site_key = _resolve_site(meta, patient)
    site = meta["sites"][site_key]
    morph = patient.get("morphology", {})
    upm = float(meta["units_per_mm"])

    zones = patient.get("zones", [])
    dome = next((z for z in zones if z.get("isAneurysm") and "dome" in z["name"].lower()),
                next((z for z in zones if z.get("isAneurysm")), {}))
    factor = _risk_factor(dome, mode)

    width_mm = max(0.1, float(morph.get("maxDiameter", 0.0)))
    neck_mm = max(0.1, float(morph.get("neckDiameterMm", 0.0) or width_mm * 0.7))
    height_mm = max(0.1, float(morph.get("aspectRatio", 1.0) or 1.0) * neck_mm)

    rx = (width_mm / 2.0) * upm
    ry = (height_mm / 2.0) * upm

    C = np.asarray(site["centre"], dtype=float)
    OUT = np.asarray(site["outward"], dtype=float)
    OUT = OUT / (np.linalg.norm(OUT) or 1.0)

    rgb = STABLE_RGB + (CRITICAL_RGB - STABLE_RGB) * factor

    return {
        "site": site_key,
        "site_centre": [float(v) for v in C],
        "outward": [float(v) for v in OUT],
        "centre": [float(v) for v in (C + OUT * (ry * 0.7))],
        "width_mm": width_mm,
        "neck_mm": neck_mm,
        "height_mm": height_mm,
        "rx": rx,
        "ry": ry,
        "risk_factor": factor,
        "rgb": [int(v) for v in np.clip(rgb, 0, 255)],
    }


def _align_z_to(points: np.ndarray, target: np.ndarray) -> np.ndarray:
    """
    Rotate points so the local +Z axis lands on `target`.

    Equivalent to Quaternion.setFromUnitVectors in the browser, via Rodrigues.
    PyVista's sphere is built with its poles on Z, whereas three.js builds its
    on Y — hence Z here and Y there. The resulting sac is identical; only the
    primitive's own convention differs.
    """
    a = np.array([0.0, 0.0, 1.0])
    b = target / (np.linalg.norm(target) or 1.0)
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    s = float(np.linalg.norm(v))
    if s < 1e-12:                      # parallel or antiparallel
        return points if c > 0 else -points
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    R = np.eye(3) + vx + vx @ vx * ((1 - c) / (s * s))
    return points @ R.T


def render(
    patient: dict[str, Any],
    out_png: Path,
    mode: str = "TAWSS",
    models_dir: Path = Path("/mnt/d/fyp/models"),
    size: tuple[int, int] = (1200, 900),
) -> dict[str, Any]:
    import pyvista as pv
    import trimesh

    meta = json.loads((models_dir / "brain.json").read_text())
    scene = trimesh.load(str(models_dir / "brain.glb"), force="scene")
    tm = max(scene.geometry.values(), key=lambda g: len(g.vertices))

    info = sac_params(meta, patient, mode)

    faces = np.hstack([np.full((len(tm.faces), 1), 3),
                       np.asarray(tm.faces)]).astype(np.int64).ravel()
    network = pv.PolyData(np.asarray(tm.vertices, dtype=float), faces)

    # Sphere scaled per axis, then rotated so its pole follows `outward` —
    # the same construction as SphereGeometry + quaternion in neuro3d.js.
    sac = pv.Sphere(radius=1.0, theta_resolution=40, phi_resolution=28)
    sac.points *= np.array([info["rx"], info["rx"], info["ry"]])
    sac.points = _align_z_to(sac.points, np.asarray(info["outward"], dtype=float))
    sac.points += np.asarray(info["centre"], dtype=float)

    pv.OFF_SCREEN = True
    pl = pv.Plotter(off_screen=True, window_size=list(size))
    pl.set_background(BG)
    pl.add_mesh(network, color=f"#{int(STABLE_RGB[0]):02X}{int(STABLE_RGB[1]):02X}"
                               f"{int(STABLE_RGB[2]):02X}",
                smooth_shading=True, specular=0.15, specular_power=12)
    pl.add_mesh(sac, color="#%02X%02X%02X" % tuple(info["rgb"]),
                smooth_shading=True, specular=0.2, specular_power=14)

    # Same framing rule as focusOn() in neuro3d.js: a FIXED distance, identical
    # for every case.
    #
    # Scaling the distance to the sac (as this first did, at radius x 14) makes
    # every aneurysm fill the same fraction of the screen — which cancels the
    # size difference that is the whole point of drawing them at true scale. An
    # 11 mm sac must look twice a 5.4 mm one, so the viewpoint has to stay put.
    # Aim at the SAC, not the anatomical site. The sac sits a little outboard
    # of the site, so targeting the site leaves it off-centre by an amount that
    # varies with its own height — different framing for every case.
    C = np.asarray(info["centre"], dtype=float)
    dist = VIEW_DISTANCE
    d = C / (np.linalg.norm(C) or 1.0)
    pl.camera.focal_point = tuple(C)
    pl.camera.position = tuple(C + d * dist + np.array([0.0, 0.12 * dist, 0.0]))
    pl.camera.up = (0.0, 0.0, 1.0)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    pl.screenshot(str(out_png), transparent_background=False)
    pl.close()

    info.update({"png": str(out_png), "bytes": out_png.stat().st_size, "mode": mode})
    return info


def render_all(patients_json: Path, models_dir: Path) -> list[dict[str, Any]]:
    data = json.loads(Path(patients_json).read_text())
    pats = data["patients"] if isinstance(data, dict) else data
    out = []
    for p in pats:
        for mode in ("TAWSS", "OSI"):
            suffix = "-osi" if mode == "OSI" else ""
            png = models_dir / f"{p['id']}{suffix}.png"
            try:
                out.append(render(p, png, mode, models_dir))
            except Exception as exc:  # noqa: BLE001
                out.append({"png": str(png), "error": f"{exc.__class__.__name__}: {exc}"})
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Render the brain view per case")
    ap.add_argument("--patients", default="/mnt/d/fyp/real-cfd-patients.json")
    ap.add_argument("--models", default="/mnt/d/fyp/models")
    args = ap.parse_args()

    for r in render_all(Path(args.patients), Path(args.models)):
        if "error" in r:
            print(f"  {Path(r['png']).name:<26} ERROR {r['error']}")
        else:
            print(f"  {Path(r['png']).name:<26} site={r['site']:<5} "
                  f"w={r['width_mm']:5.2f} neck={r['neck_mm']:5.2f} "
                  f"h={r['height_mm']:5.2f} mm  risk={r['risk_factor']:.2f}  "
                  f"rgb={tuple(r['rgb'])}")
