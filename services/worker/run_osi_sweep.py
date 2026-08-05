"""
Calibration points for OSI — the ones that need a cardiac cycle.

WHY THIS EXISTS SEPARATELY FROM run_sweep.py
run_sweep.py solves STEADY, which is why it can afford ten design points: each
converges in minutes. But OSI is defined over a cycle,

    OSI = 0.5 * (1 - |mean(tau)| / mean|tau|),

and on a steady solve the two averages are the same vector, so OSI is
identically zero by construction. No amount of steady solving produces it.

THE PROBLEM THIS FIXES
With only three transient solves available, OSI had to be fitted as a power law
in sac TAWSS — the one predictor there were enough points to support. That has
a consequence which is easy to miss and impossible to defend once seen:

    OSI  = k * TAWSS^0.781          (the fit)
    ECAP = OSI / TAWSS = k * TAWSS^-0.219

An exponent of -0.219 is close enough to zero that ECAP barely moves: over the
entire calibrated diameter range it spans 0.037 to 0.044. Reported to two
decimals every case reads "0.04", and a user is entitled to conclude the number
is hardcoded, because functionally it is. The fit did not discover that ECAP is
constant; the CHOICE of predictor forced it to be.

The fix is more transient points spanning a wider geometry range, so OSI can be
fitted against the geometry itself and ECAP is free to vary with it.

COST
A coarse pulsatile case (~0.7 mm base cells, one refinement level, 2 prism
layers) reaches a full cycle in roughly 1.5-2 h on three ranks, against ~10 h
for the fine mesh. That trade is defensible for OSI specifically and NOT for
TAWSS: wall shear is a near-wall gradient and is the quantity most damaged by
coarsening, whereas OSI is a ratio of two averages of the same field, so
first-order mesh error largely cancels. TAWSS stays calibrated on the fine
steady sweep; only OSI is taken from here.

    python run_osi_sweep.py --nproc 3 --jobs 3
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "pipeline"))

from geometry import AneurysmGeometry            # noqa: E402
from run_cohort import build_case, sh            # noqa: E402

# Design points, chosen to WIDEN the range rather than to fill it in. The three
# existing transient solves sit at 5.38, 8.0 and one unrecorded diameter, all
# bunched in the middle; adding more points there would not tell the fit
# anything it does not already know. These bracket the clinical range instead.
SWEEP: list[float] = [0.0018, 0.0046, 0.0060]     # nominal domes 3.6, 9.2, 12.0 mm

# One cardiac cycle. The steady warm start removes most of the start-up
# transient, so a single cycle is usable — see the averaging window below.
CYCLE_S = 0.90
AVG_START_S = 0.10

# Ford 2005 / Hoi 2010 ICA waveform, mean ~4.6 mL/s over T = 0.9 s. Identical to
# the table used by the fine solves, so these points sit on the same physics.
INLET_TABLE = """    inlet
    {
        type            flowRateInletVelocity;
        volumetricFlowRate table
        (
            (0.00  4.20e-06) (0.05  5.60e-06) (0.10  6.75e-06)
            (0.15  7.00e-06) (0.20  6.30e-06) (0.25  5.30e-06)
            (0.30  4.60e-06) (0.35  4.20e-06) (0.40  4.05e-06)
            (0.45  4.10e-06) (0.50  4.25e-06) (0.55  4.30e-06)
            (0.60  4.20e-06) (0.65  4.05e-06) (0.70  3.95e-06)
            (0.75  3.90e-06) (0.80  3.95e-06) (0.85  4.05e-06)
            (0.90  4.20e-06)
        );
        extrapolateProfile false;
        value           uniform (0 0 0);
    }"""

# `mag` BEFORE `fieldAverage` for TAWSS, and the raw vector averaged separately
# for the OSI numerator. Averaging the vector alone yields |mean(tau)| and would
# silently produce the wrong half of the definition.
PULSATILE_CONTROL = f"""FoamFile {{ version 2.0; format ascii; class dictionary; object controlDict; }}
application     pimpleFoam;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         {CYCLE_S:.2f};
deltaT          1e-5;
writeControl    adjustableRunTime;
writeInterval   0.05;
purgeWrite      3;
writeFormat     binary;
writePrecision  8;
runTimeModifiable true;
adjustTimeStep  yes;
maxCo           0.9;
maxDeltaT       5e-4;
functions
{{
    wallShearStress
    {{
        type            wallShearStress;
        libs            (fieldFunctionObjects);
        patches         (wall wall_aneurysm);
        executeControl  timeStep;
        writeControl    writeTime;
        log             false;
    }}
    magWss
    {{
        type            mag;
        libs            (fieldFunctionObjects);
        field           wallShearStress;
        result          magWallShearStress;
        executeControl  timeStep;
        writeControl    writeTime;
        log             false;
    }}
    averages
    {{
        type            fieldAverage;
        libs            (fieldFunctionObjects);
        timeStart       {AVG_START_S:.2f};
        executeControl  timeStep;
        writeControl    writeTime;
        fields
        (
            wallShearStress    {{ mean on; prime2Mean off; base time; }}
            magWallShearStress {{ mean on; prime2Mean off; base time; }}
        );
    }}
}}
"""

PULSATILE_SCHEMES = """FoamFile { version 2.0; format ascii; class dictionary; object fvSchemes; }
ddtSchemes      { default Euler; }
gradSchemes     { default Gauss linear; grad(U) cellLimited Gauss linear 1; }
divSchemes      { default none; div(phi,U) Gauss linearUpwind grad(U);
                  div((nuEff*dev2(T(grad(U))))) Gauss linear; }
laplacianSchemes{ default Gauss linear corrected; }
interpolationSchemes { default linear; }
snGradSchemes   { default corrected; }
"""

PULSATILE_SOLUTION = """FoamFile { version 2.0; format ascii; class dictionary; object fvSolution; }
solvers
{
    p     { solver GAMG; tolerance 1e-7; relTol 0.01; smoother GaussSeidel;
            nCellsInCoarsestLevel 100; }
    pFinal{ $p; relTol 0; }
    "(U|k|omega)" { solver smoothSolver; smoother symGaussSeidel;
                    tolerance 1e-8; relTol 0.01; }
    "(U|k|omega)Final" { $U; relTol 0; }
}
PIMPLE
{
    nOuterCorrectors 2;
    nCorrectors      2;
    nNonOrthogonalCorrectors 1;
}
"""


def coarsen(case_dir: Path) -> None:
    """
    Drop the mesh from 0.4 mm to 0.7 mm base cells and one refinement level.

    The substitutions are NOT anchored to line start for the block bounds: the
    template packs min and max on one line ("xMin -0.003;  xMax 0.103;"), and an
    anchored pattern leaves the max values stale, producing an inside-out block
    that blockMesh accepts and snappyHexMesh then fails on obscurely.
    """
    bm_path = case_dir / "system" / "blockMeshDict"
    bm = bm_path.read_text()
    for key in ("nx", "ny", "nz"):
        m = re.search(rf"^{key}\s+(\d+);", bm, flags=re.M)
        if not m:
            raise RuntimeError(f"{key} not found in blockMeshDict")
        coarse = max(8, int(round(int(m.group(1)) * 0.4 / 0.7)))
        bm = re.sub(rf"^{key}\s+\d+;", f"{key} {coarse};", bm, flags=re.M)
    bm_path.write_text(bm)

    shm_path = case_dir / "system" / "snappyHexMeshDict"
    shm = shm_path.read_text()
    shm = shm.replace("level (1 2);", "level (1 1);")
    shm = shm.replace("nSurfaceLayers 3;", "nSurfaceLayers 2;")
    shm_path.write_text(shm)


def to_pulsatile(case_dir: Path, nproc: int) -> None:
    """Swap the converged steady case over to one pulsatile cardiac cycle."""
    latest = max(
        (d for d in case_dir.iterdir()
         if d.is_dir() and d.name not in ("0", "constant", "system")
         and re.fullmatch(r"[0-9.eE+-]+", d.name)),
        key=lambda d: float(d.name),
        default=None,
    )
    if latest is None:
        raise RuntimeError("no steady time directory to warm-start from")

    # Seed t=0 with the converged steady field, then put the pulsatile inlet
    # back: the steady solution carries the CONSTANT inlet condition with it,
    # and copying it over 0/U silently reverts the waveform.
    for fld in ("U", "p"):
        src = latest / fld
        if src.exists():
            shutil.copy(src, case_dir / "0" / fld)

    # foamFormatConvert converts to whatever writeFormat controlDict names, so
    # the ascii dictionary has to be in place BEFORE it runs. Converting first
    # and swapping after is a no-op — the steady controlDict says binary, so the
    # field stays binary and the inlet regex below hits raw bytes and dies on a
    # UnicodeDecodeError partway through the header.
    control = case_dir / "system" / "controlDict"
    control.write_text(PULSATILE_CONTROL.replace("writeFormat     binary;",
                                                 "writeFormat     ascii;"))
    sh("foamFormatConvert -time 0", case_dir, case_dir / "log.formatConvert")

    u_path = case_dir / "0" / "U"
    u = u_path.read_text()
    u, n = re.subn(r"    inlet\s*\n    \{.*?\n    \}", INLET_TABLE, u, count=1, flags=re.S)
    if n != 1:
        raise RuntimeError("inlet block not replaced in 0/U")
    u_path.write_text(u)

    # Back to binary for the solve itself: a cycle writes ~18 times and ascii
    # fields are several times the size for no benefit once nothing needs to
    # read them as text.
    control.write_text(PULSATILE_CONTROL)
    (case_dir / "system" / "fvSchemes").write_text(PULSATILE_SCHEMES)
    (case_dir / "system" / "fvSolution").write_text(PULSATILE_SOLUTION)

    # Old steady time directories would otherwise be picked up as the "latest"
    # result and reconstructed instead of the cycle.
    for d in case_dir.iterdir():
        if d.is_dir() and re.fullmatch(r"[0-9.eE+-]+", d.name) and d.name != "0":
            shutil.rmtree(d)
    shutil.rmtree(case_dir / "processor0", ignore_errors=True)
    for d in case_dir.glob("processor*"):
        shutil.rmtree(d, ignore_errors=True)

    dp = case_dir / "system" / "decomposeParDict"
    dp.write_text(re.sub(r"numberOfSubdomains\s+\d+;",
                         f"numberOfSubdomains {nproc};", dp.read_text()))


def cycle_fraction(case_dir: Path) -> float:
    """How much of the cycle the averages actually cover, from the solver log."""
    log = case_dir / "log.pimpleFoam"
    if not log.exists():
        return 0.0
    times = re.findall(r"^Time = ([0-9.eE+-]+)", log.read_text(errors="ignore"), flags=re.M)
    if not times:
        return 0.0
    reached = float(times[-1])
    return max(0.0, min(1.0, (reached - AVG_START_S) / (CYCLE_S - AVG_START_S)))


def run_one(root: Path, sac_r: float, nproc: int) -> dict[str, Any]:
    from export_patient import build_patient

    name = f"osi_r{int(sac_r * 1e5):04d}"
    case_dir = root / name
    geom = AneurysmGeometry(sac_radius=sac_r)

    build_case(case_dir, geom, nproc)
    coarsen(case_dir)

    for step, cmd in (
        ("blockMesh", "blockMesh"),
        ("surfaceFeatureExtract", "surfaceFeatureExtract"),
        ("snappyHexMesh", "snappyHexMesh -overwrite"),
        ("checkMesh", "checkMesh -constant"),
        ("decomposePar", "decomposePar -force"),
        ("simpleFoam", f"mpirun --use-hwthread-cpus -np {nproc} simpleFoam -parallel"),
        ("reconstructPar", "reconstructPar -latestTime"),
    ):
        if not sh(cmd, case_dir, case_dir / f"log.{step}") and step != "checkMesh":
            raise RuntimeError(f"{name}: {step} failed")

    to_pulsatile(case_dir, nproc)
    sh("decomposePar -force", case_dir, case_dir / "log.decomposePar2")
    # sh() defaults to a two-hour cap, which is roughly the expected runtime —
    # a case that ran 1h55m would be killed just short of the cycle and its
    # averages would cover a fraction too small to use.
    sh(f"mpirun --use-hwthread-cpus -np {nproc} pimpleFoam -parallel",
       case_dir, case_dir / "log.pimpleFoam", timeout=6 * 3600)
    sh("reconstructPar -latestTime", case_dir, case_dir / "log.reconstructPar2")

    frac = cycle_fraction(case_dir)
    rec = build_patient(case_dir, name, {"age": 60, "hypertension": False,
                                         "earlierSAH": False, "population": "Other",
                                         "site": "ICA"})
    z = {x["name"]: x for x in rec["zones"]}
    m, h = rec["morphology"], rec["hemodynamics"]
    sac_osi = z["Aneurysm Dome"]["osi"]
    if not sac_osi > 0:
        raise RuntimeError(f"{name}: OSI came out {sac_osi} — the cycle averages "
                           f"are missing, so this point would be a fabrication")

    return {
        "case": name,
        "sac_radius_m": sac_r,
        "dome_mm": m["maxDiameter"],
        "neck_mm": m["neckDiameterMm"],
        "aspect_ratio": m["aspectRatio"],
        "sac_tawss_pa": z["Aneurysm Dome"]["tawss"],
        "sac_osi": sac_osi,
        "sac_ecap": h.get("ecap"),
        "parent_tawss_pa": z["Parent Artery Inlet"]["tawss"],
        "parent_osi": z["Parent Artery Inlet"]["osi"],
        "nwss": h["nwss"],
        "avg_window": [AVG_START_S, CYCLE_S],
        "cycle_fraction": round(frac, 3),
        "mesh": "coarse (0.7 mm base, 1 refinement level, 2 layers)",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Transient sweep for OSI calibration")
    ap.add_argument("--root", default="~/cases/osi")
    ap.add_argument("--out", default="/mnt/d/fyp/services/worker/models/pulsatile_points.json")
    ap.add_argument("--nproc", type=int, default=3, help="MPI ranks per case")
    ap.add_argument("--jobs", type=int, default=3, help="cases solved concurrently")
    a = ap.parse_args()

    root = Path(a.root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    out = Path(a.out)

    existing = json.loads(out.read_text()).get("points", []) if out.exists() else []
    done = {p["case"] for p in existing}
    todo = [r for r in SWEEP if f"osi_r{int(r * 1e5):04d}" not in done]
    print(f"{len(existing)} existing point(s); {len(todo)} to solve "
          f"({a.jobs} at a time, {a.nproc} ranks each)", flush=True)

    def attempt(sac_r: float) -> dict[str, Any] | None:
        try:
            pt = run_one(root, sac_r, a.nproc)
            print(f"  {pt['case']}: dome {pt['dome_mm']:.2f} mm, "
                  f"TAWSS {pt['sac_tawss_pa']:.4f} Pa, OSI {pt['sac_osi']:.5f}, "
                  f"cycle {pt['cycle_fraction']:.0%}", flush=True)
            return pt
        except Exception as exc:                       # noqa: BLE001
            print(f"  r={sac_r * 1e3:.1f} mm FAILED: {exc.__class__.__name__}: {exc}",
                  flush=True)
            traceback.print_exc()
            return None

    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        for pt in pool.map(attempt, todo):
            if pt is None:
                continue
            existing.append(pt)
            # Written after every point: two hours of solving must not be lost
            # because the last case diverged.
            out.write_text(json.dumps({
                "generatedAt": datetime.now().isoformat(timespec="seconds"),
                "solver": "OpenFOAM ESI v2412 pimpleFoam (transient, 1 cardiac cycle)",
                "note": "Every point is a real transient solve. OSI cannot be "
                        "obtained from a steady solution — it is identically zero "
                        "there by construction.",
                "points": existing,
            }, indent=2))

    print(f"\n{len(existing)} transient point(s) in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
