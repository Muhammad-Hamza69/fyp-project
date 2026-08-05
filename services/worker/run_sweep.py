"""
Generate the calibration set for the fast hemodynamic surrogate.

WHY
A full Navier-Stokes solve takes hours — ~10 h for one cardiac cycle at 239k
cells on this hardware. That is the irreducible cost of the physics, and it is
not something a web upload can wait for. But the expensive part only has to be
paid ONCE per point in the design space, not once per user: a surrogate fitted
to solved cases evaluates in microseconds.

This runs many CHEAP steady solves across the geometry family and records what
each one produced. Steady is the right choice here: at 239k cells it converges
in ~2.5 minutes against ~10 hours for the pulsatile equivalent, and it fixes
TAWSS, NWSS, RRT and LSAR — everything except OSI and ECAP, which genuinely
require a cycle and are handled separately.

WHAT THIS IS NOT
It is not a replacement for solving. Every point here IS a real OpenFOAM
solution; the surrogate interpolates between them. Outside the range swept it
extrapolates, and surrogate.py reports when a query falls outside the hull
rather than quietly guessing.

    python run_sweep.py --nproc 4 --out /mnt/d/fyp/services/worker/models/calibration.json
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "pipeline"))

from geometry import AneurysmGeometry            # noqa: E402
from run_cohort import build_case, solve         # noqa: E402
from export_patient import build_patient         # noqa: E402

# Design points. sac_radius dominates the response — it sets how far the
# recirculation sits from the parent flow — so it is sampled most densely.
# neck_offset controls how open the sac is to the parent artery, which is the
# second-order effect.
#
# The range spans 3–12 mm dome diameter, covering the clinical range in which
# treatment decisions are actually made.
SWEEP: list[tuple[float, float]] = [
    (0.0015, 0.0004), (0.0020, 0.0004), (0.0026, 0.0004),
    (0.0032, 0.0010), (0.0040, 0.0014), (0.0046, 0.0018),
    (0.0055, 0.0030), (0.0060, 0.0030),
    # Second-order: same sac, different neck openness.
    (0.0040, 0.0004), (0.0040, 0.0030),
]

DEMOGRAPHICS = {"age": 60, "hypertension": False, "earlierSAH": False,
                "population": "Other", "site": "ICA"}


def run(root: Path, out: Path, nproc: int, resume: bool) -> int:
    root.mkdir(parents=True, exist_ok=True)
    points: list[dict[str, Any]] = []

    if resume and out.exists():
        points = json.loads(out.read_text()).get("points", [])
        print(f"resuming with {len(points)} existing point(s)")
    done = {p["case"] for p in points}

    for i, (sac_r, neck_off) in enumerate(SWEEP):
        name = f"sweep_r{int(sac_r*1e5):04d}_n{int(neck_off*1e5):04d}"
        if name in done:
            print(f"[{i+1}/{len(SWEEP)}] {name} — already have it")
            continue

        print(f"[{i+1}/{len(SWEEP)}] {name}: sac r={sac_r*1e3:.2f} mm, "
              f"neck offset={neck_off*1e3:.2f} mm", flush=True)
        case_dir = root / name
        try:
            geom = AneurysmGeometry(sac_radius=sac_r, neck_offset=neck_off)
            build_case(case_dir, geom, nproc)
            solve(case_dir, nproc)
            rec = build_patient(case_dir, name, DEMOGRAPHICS)

            z = {x["name"]: x for x in rec["zones"]}
            m, h = rec["morphology"], rec["hemodynamics"]
            pt = {
                "case": name,
                "sac_radius_m": sac_r,
                "neck_offset_m": neck_off,
                # Inputs a real upload could supply after segmentation.
                "max_diameter_mm": m["maxDiameter"],
                "neck_diameter_mm": m["neckDiameterMm"],
                "aspect_ratio": m["aspectRatio"],
                "dome_to_neck": m["domeToNeck"],
                "volume_mm3": m["volumeMm3"],
                "non_sphericity_index": m["nonSphericityIndex"],
                # Solved outputs.
                "parent_tawss_pa": z["Parent Artery Inlet"]["tawss"],
                "sac_tawss_pa": z["Aneurysm Dome"]["tawss"],
                "neck_tawss_pa": z["Aneurysm Neck"]["tawss"],
                "nwss": h["nwss"],
                "rrt": h["rrt"],
                "lsar_relative": h["lsarRelative"],
                "lsar_absolute": h["lsarAbsolute"],
                "wss_max_pa": h["wssMaxPa"],
                "mesh_cells": rec["provenance"].get("meshCells", ""),
            }
            points.append(pt)
            print(f"    -> parent {pt['parent_tawss_pa']:.3f} Pa, "
                  f"sac {pt['sac_tawss_pa']:.4f} Pa, NWSS {pt['nwss']:.4f}, "
                  f"RRT {pt['rrt']:.2f}", flush=True)

            # Written after every point: an hour of solving must not be lost
            # because point 9 of 10 failed.
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({
                "generatedAt": datetime.now().isoformat(timespec="seconds"),
                "solver": "OpenFOAM ESI v2412 simpleFoam (steady)",
                "note": "Each point is a real CFD solution. The surrogate "
                        "interpolates between them; it does not replace them.",
                "points": points,
            }, indent=2))
        except Exception as exc:                       # noqa: BLE001
            print(f"    !! {name} failed: {exc.__class__.__name__}: {exc}", flush=True)
            traceback.print_exc()

    print(f"\n{len(points)} calibration point(s) in {out}")
    return 0 if points else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Sweep the geometry family for surrogate calibration")
    ap.add_argument("--root", default="~/cases/sweep")
    ap.add_argument("--out", default="/mnt/d/fyp/services/worker/models/calibration.json")
    ap.add_argument("--nproc", type=int, default=4)
    ap.add_argument("--no-resume", action="store_true")
    a = ap.parse_args()
    raise SystemExit(run(Path(a.root).expanduser(), Path(a.out), a.nproc, not a.no_resume))
