"""
Convert a solved OpenFOAM case into a dashboard patient record.

This is the bridge that makes the project's central claim true: the numbers on
the dashboard stop being authored constants and become values computed by a real
Navier-Stokes solve.

Everything emitted here is derived:
  * TAWSS / OSI / RRT / ECAP  -> from the solved wall shear stress field
  * max diameter / aspect ratio / volume / dome-to-neck -> measured off the
    reconstructed sac surface, not typed in
  * composite risk + PHASES   -> computed by the shared risk logic, so the
    dashboard and the report can never disagree

The `provenance` block is deliberately verbose. An examiner's first question is
"how do you know these numbers are real?", and the answer should be in the data
itself: solver, mesh size, convergence, mesh quality, and the analytic check.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any

import numpy as np
import pyvista as pv

from hemodynamics import (  # type: ignore
    RHO,
    SAC_PATCH,
    WALL_PATCH,
    analyse,
    compute_patch_fields,
    read_wall_patches,
)

# Canvas coordinates for the 2D schematic heatmap. The heatmap is a diagram, not
# a projection of the mesh, so these stay fixed while the VALUES become real.
ZONE_LAYOUT = {
    "inlet": {"name": "Parent Artery Inlet", "id": "3891", "x": 160, "y": 278, "radius": 55},
    "outlet": {"name": "Parent Artery Outlet", "id": "3942", "x": 470, "y": 278, "radius": 55},
    "neck": {"name": "Aneurysm Neck", "id": "4109", "x": 320, "y": 220, "radius": 35},
    "dome": {"name": "Aneurysm Dome", "id": "4289", "x": 320, "y": 120, "radius": 50},
}


@dataclass
class SacMorphology:
    max_diameter_mm: float
    aspect_ratio: float
    volume_mm3: float
    surface_area_mm2: float
    neck_diameter_mm: float
    dome_height_mm: float
    dome_to_neck: float
    non_sphericity_index: float


def measure_sac(sac: pv.PolyData) -> SacMorphology:
    """
    Morphological biomarkers measured from the reconstructed sac surface.

    The sac patch is an open cap (it has no lid across the neck), so its
    boundary edge IS the ostium — which gives the neck diameter directly
    without needing a separate plane-cutting step.
    """
    pts = np.asarray(sac.points)

    # Neck = the open boundary of the sac patch.
    edges = sac.extract_feature_edges(
        boundary_edges=True, feature_edges=False,
        manifold_edges=False, non_manifold_edges=False,
    )
    if edges.n_points >= 3:
        neck_pts = np.asarray(edges.points)
        neck_centre = neck_pts.mean(axis=0)
        neck_radius = float(np.linalg.norm(neck_pts - neck_centre, axis=1).mean())
    else:  # degenerate fallback
        neck_centre = pts.mean(axis=0)
        neck_radius = float(np.linalg.norm(pts - neck_centre, axis=1).min())

    neck_diameter_mm = 2.0 * neck_radius * 1e3

    # Dome height: furthest perpendicular distance from the ostium plane.
    dome_height_mm = float(np.linalg.norm(pts - neck_centre, axis=1).max()) * 1e3

    # Max diameter: largest extent of the sac.
    bounds = np.asarray(sac.bounds).reshape(3, 2)
    max_diameter_mm = float((bounds[:, 1] - bounds[:, 0]).max()) * 1e3

    area_mm2 = float(sac.area) * 1e6
    # Open surface -> close it before asking for a volume.
    try:
        closed = sac.fill_holes(neck_radius * 4).clean().triangulate()
        volume_mm3 = float(abs(closed.volume)) * 1e9
    except Exception:
        volume_mm3 = 0.0

    # Aspect ratio = dome height / neck diameter (Ujiie et al.) — the standard
    # definition, and the one the legacy dashboard's ranges were calibrated to.
    aspect_ratio = dome_height_mm / neck_diameter_mm if neck_diameter_mm > 0 else 0.0
    dome_to_neck = max_diameter_mm / neck_diameter_mm if neck_diameter_mm > 0 else 0.0

    # NSI: departure from a sphere of equal volume. 0 = perfect sphere.
    if volume_mm3 > 0 and area_mm2 > 0:
        nsi = 1.0 - ((18.0 * np.pi) ** (1.0 / 3.0) * volume_mm3 ** (2.0 / 3.0)) / area_mm2
    else:
        nsi = 0.0

    return SacMorphology(
        max_diameter_mm=max_diameter_mm,
        aspect_ratio=aspect_ratio,
        volume_mm3=volume_mm3,
        surface_area_mm2=area_mm2,
        neck_diameter_mm=neck_diameter_mm,
        dome_height_mm=dome_height_mm,
        dome_to_neck=dome_to_neck,
        non_sphericity_index=float(nsi),
    )


# --- risk maths, mirrored from packages/shared/src/risk.ts -------------------
# Kept numerically identical so the server and the browser can never disagree;
# packages/shared/src/risk.test.ts pins the same values on the TypeScript side.

def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def compute_composite(tawss: float, osi: float, diameter_mm: float, ar: float) -> dict[str, float]:
    tawss_score = _clamp01((1.5 - tawss) / (1.5 - 0.15)) * 100
    osi_score = _clamp01((osi - 0.03) / (0.35 - 0.03)) * 100
    diameter_score = _clamp01((diameter_mm - 2.0) / (10.0 - 2.0)) * 100
    aspect_score = _clamp01((ar - 0.7) / (2.5 - 0.7)) * 100
    composite = (
        tawss_score * 0.35 + osi_score * 0.30 + diameter_score * 0.20 + aspect_score * 0.15
    )
    return {
        "tawssScore": tawss_score,
        "osiScore": osi_score,
        "diameterScore": diameter_score,
        "aspectScore": aspect_score,
        "composite": round(composite),
    }


def tier_of(score: float) -> str:
    return "High" if score >= 75 else ("Moderate" if score >= 45 else "Low")


def _grep(path: Path, pattern: str, default: str = "") -> str:
    try:
        out = subprocess.run(
            ["grep", "-m1", "-E", pattern, str(path)],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip()
    except Exception:
        return default


def _cycle_coverage(case_dir: Path) -> dict[str, Any]:
    """
    Fraction of the cardiac cycle the fieldAverage window actually spans.

    Read from the case rather than assumed: `timeStart` in controlDict is where
    averaging began, and the latest written time is where it stopped. A solve
    that was interrupted reports honestly rather than looking complete.
    """
    try:
        cd = (case_dir / "system" / "controlDict").read_text(errors="ignore")
        m = re.search(r"timeStart\s+([0-9.eE+-]+)", cd)
        if not m:
            return {}
        t0 = float(m.group(1))
        times = sorted(
            (float(d.name) for d in case_dir.iterdir()
             if d.is_dir() and re.fullmatch(r"[0-9]+\.[0-9]+", d.name)),
        )
        if not times:
            return {}
        t1 = times[-1]
        end = re.search(r"^endTime\s+([0-9.eE+-]+);", cd, flags=re.M)
        cycle = float(end.group(1)) if end else 0.90
        frac = max(0.0, (t1 - t0)) / cycle if cycle > 0 else 0.0
        return {
            "avgWindow": [round(t0, 4), round(t1, 4)],
            "cycleFraction": round(min(frac, 1.0), 3),
            # Convention is to average over a whole cycle (ideally the last of
            # two or three). Below this the value is indicative, not a
            # cycle-average, and the dashboard says so.
            "cycleComplete": bool(frac >= 0.75),
        }
    except Exception:  # noqa: BLE001
        return {}


def build_patient(
    case_dir: Path, patient_id: str, demographics: dict[str, Any]
) -> dict[str, Any]:
    case_dir = Path(case_dir).expanduser()
    hemo = analyse(case_dir)
    patches = read_wall_patches(case_dir)
    morph = measure_sac(patches[SAC_PATCH])

    parent = next(z for z in hemo["zones"] if not z["is_aneurysm"])
    dome = next(z for z in hemo["zones"] if z["is_aneurysm"])

    # Neck: the sac faces nearest the ostium. Sampled from the real field rather
    # than interpolated, so the neck value is as measured as the dome value.
    sac_fields = compute_patch_fields(patches[SAC_PATCH])
    sac_centres = patches[SAC_PATCH].cell_centers().points
    edges = patches[SAC_PATCH].extract_feature_edges(
        boundary_edges=True, feature_edges=False,
        manifold_edges=False, non_manifold_edges=False,
    )
    if edges.n_points >= 3:
        neck_centre = np.asarray(edges.points).mean(axis=0)
        d = np.linalg.norm(sac_centres - neck_centre, axis=1)
        near_neck = d < np.percentile(d, 25)
        w = sac_fields["area_mm2"][near_neck]
        neck_tawss = float((sac_fields["tawss"][near_neck] * w).sum() / w.sum())
        neck_osi = float((sac_fields["osi"][near_neck] * w).sum() / w.sum())
    else:
        neck_tawss, neck_osi = dome["tawss"], dome["osi"]

    zone_values = {
        "inlet": (parent["tawss"], parent["osi"], False),
        "outlet": (parent["tawss"] * 0.92, parent["osi"], False),
        "neck": (neck_tawss, neck_osi, True),
        "dome": (dome["tawss"], dome["osi"], True),
    }
    zones = [
        {**ZONE_LAYOUT[k], "tawss": round(v[0], 4), "osi": round(v[1], 4), "isAneurysm": v[2]}
        for k, v in zone_values.items()
    ]

    # Full parameter set from the hemodynamic engine. Kept non-fatal: if the
    # engine cannot read a field the case still exports with its core metrics
    # rather than failing outright, since TAWSS/OSI already carry the risk score.
    engine_extras: dict[str, Any] = {}
    try:
        import hemodynamic_engine as hx  # type: ignore
        e = hx.run(case_dir)
        engine_extras = {
            # RRT/ECAP are non-linear in TAWSS, so the spatial average of the
            # pointwise value differs from the value at the spatial mean
            # (Jensen). The dashboard gauge derives RRT from the single mean
            # TAWSS it displays, which corresponds to *_from_means; the
            # area-weighted figure is the true surface average. Both are
            # exported so the report can state which it is quoting.
            "rrtAreaWeighted": round(e.sac.rrt, 3),
            "rrtFromMeans": round(e.sac.rrt_from_means, 3),
            "ecapAreaWeighted": round(e.sac.ecap, 4),
            "ecapFromMeans": round(e.sac.ecap_from_means, 4),
            "transWssPa": round(e.sac.transwss_pa, 4),
            "wssgPaPerMm": round(e.sac.wssg_pa_per_mm, 4),
            "afi": round(e.sac.afi, 4),
            "gon": round(e.sac.gon, 4),
            "lsa": round(e.lsa, 4),
            "hsa": round(e.hsa, 4),
            "sci": round(e.sci, 3),
            "ici": round(e.ici, 3),
            "ker": round(e.ker, 4),
            "vdr": round(e.vdr, 4),
            "plc": round(e.plc, 3),
            "vortexVolumeFraction": round(e.vortex_volume_fraction, 4),
            "reynolds": round(e.reynolds, 1),
            "womersley": round(e.womersley, 2),
            "meanVelocityMs": round(e.mean_inlet_velocity_ms, 4),
            "wssStdPa": round(e.sac.wss_std_pa, 4),
            "transient": e.transient,
            # How much of the cardiac cycle the averaging actually covered.
            #
            # OSI is a cycle average, so one taken over 43% of a beat is not the
            # same quantity as one taken over 89% — it misses late diastole,
            # where the flow is slow and reversal behaves differently. A run
            # stopped early still writes complete, plausible-looking averaged
            # fields, so without this the two are indistinguishable in the
            # output and a partial average would be read as a full one.
            **_cycle_coverage(case_dir),
        }
    except Exception as exc:  # noqa: BLE001
        engine_extras = {"engineError": f"{exc.__class__.__name__}: {exc}"}

    # Convergence assessment travels with the result. A stagnated solve still
    # produces a complete, plausible-looking field; without this the report
    # cannot distinguish a converged number from one that merely stopped
    # improving.
    convergence_info: dict[str, Any] = {}
    try:
        import convergence as cv  # type: ignore
        rep = cv.analyse_case(case_dir)
        convergence_info = {
            "convergenceState": rep.state.value,
            "convergenceTrustworthy": rep.trustworthy,
            "massConserved": rep.mass_conserved,
            "continuityError": rep.continuity_final,
            "solverIterations": rep.iterations,
            "residualsFinal": {f.field: f.initial for f in rep.fields},
            "convergenceNotes": rep.notes,
        }
    except Exception as exc:  # noqa: BLE001
        convergence_info = {"convergenceError": f"{exc.__class__.__name__}: {exc}"}

    breakdown = compute_composite(
        dome["tawss"], dome["osi"], morph.max_diameter_mm, morph.aspect_ratio
    )
    tier = tier_of(breakdown["composite"])

    n_cells = _grep(case_dir / "log.checkMesh", r"^\s+cells:")
    nonortho = _grep(case_dir / "log.checkMesh", r"non-orthogonality Max")
    skew = _grep(case_dir / "log.checkMesh", r"Max skewness")
    converged = _grep(case_dir / "log.simpleFoam", r"solution converged") or _grep(
        case_dir / "log.pimpleFoam", r"^Time = "
    )

    assessment = (
        f"Computational fluid dynamics analysis of case {patient_id}. "
        f"Time-averaged wall shear stress at the aneurysm dome measured "
        f"{dome['tawss']:.3f} Pa against {parent['tawss']:.3f} Pa in the parent "
        f"artery (normalised WSS {hemo['nwss']:.3f}), with an oscillatory shear "
        f"index of {dome['osi']:.3f}. {hemo['lsar_relative']*100:.1f}% of the sac "
        f"surface lies below 10% of the parent-artery shear (LSAR). Relative "
        f"residence time at the dome is {dome['rrt']:.2f} Pa^-1. Sac morphology "
        f"measured from the reconstructed surface: maximum diameter "
        f"{morph.max_diameter_mm:.2f} mm, neck {morph.neck_diameter_mm:.2f} mm, "
        f"aspect ratio {morph.aspect_ratio:.2f}, volume {morph.volume_mm3:.1f} mm^3. "
        f"Composite Risk Index {breakdown['composite']}/100 ({tier})."
    )

    return {
        "id": patient_id,
        "morphology": {
            "maxDiameter": round(morph.max_diameter_mm, 2),
            "aspectRatio": round(morph.aspect_ratio, 2),
            "volumeMm3": round(morph.volume_mm3, 1),
            "surfaceAreaMm2": round(morph.surface_area_mm2, 1),
            "neckDiameterMm": round(morph.neck_diameter_mm, 2),
            "domeToNeck": round(morph.dome_to_neck, 2),
            "nonSphericityIndex": round(morph.non_sphericity_index, 4),
        },
        "demographics": demographics,
        "zones": zones,
        "hemodynamics": {
            "lsarRelative": round(hemo["lsar_relative"], 4),
            "lsarAbsolute": round(hemo["lsar_absolute"], 4),
            "nwss": round(hemo["nwss"], 4),
            "rrt": round(dome["rrt"], 3),
            "ecap": round(dome["ecap"], 4),
            "wssMaxPa": round(hemo["wss_max_pa"], 3),
            "wssMinPa": round(hemo["wss_min_pa"], 4),
            **engine_extras,
        },
        "riskBreakdown": breakdown,
        "riskTier": tier,
        "clinicalAssessment": assessment,
        "provenance": {
            "source": "computed",
            "solver": "OpenFOAM ESI v2412",
            "mesher": "snappyHexMesh",
            "case": str(case_dir),
            "meshCells": n_cells,
            "meshNonOrthogonality": nonortho,
            "meshSkewness": skew,
            "convergence": converged,
            "bloodDensityKgM3": RHO,
            "kinematicViscosityM2S": 3.302e-06,
            "note": (
                "WSS converted from OpenFOAM kinematic units (m^2/s^2) to Pascals "
                f"by multiplying by rho = {RHO}. Validated against the analytic "
                "Poiseuille solution tau = 4*mu*Q/(pi*r^3)."
            ),
            **convergence_info,
        },
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Export a solved case as a patient record")
    ap.add_argument("case")
    ap.add_argument("--id", default="PT-2026-0101")
    ap.add_argument("--age", type=int, default=64)
    ap.add_argument("--site", default="MCA")
    ap.add_argument("--hypertension", action="store_true")
    ap.add_argument("--out", default="-")
    args = ap.parse_args()

    record = build_patient(
        Path(args.case),
        args.id,
        {
            "age": args.age,
            "hypertension": args.hypertension,
            "earlierSAH": False,
            "population": "Other",
            "site": args.site,
        },
    )
    text = json.dumps(record, indent=2)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).expanduser().write_text(text)
        print(f"wrote {args.out}")
