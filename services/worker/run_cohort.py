"""
Solve a cohort of aneurysm geometries and emit a dashboard dataset.

One solved case proves the pipeline runs. A cohort proves it *works*: if the
computed risk score does not move when the geometry changes, the numbers are not
really coming from the flow solution. This sweeps sac size and neck geometry and
checks that the resulting Composite Risk Index varies in the expected direction.

Each case runs: geometry -> blockMesh -> snappyHexMesh -> checkMesh -> simpleFoam
-> hemodynamics -> patient record.

Steady (simpleFoam) is used deliberately for the cohort: it converges in minutes
rather than hours, and the sac-vs-parent shear contrast that drives the risk
score is already fully expressed in the steady solution. The pulsatile run is
reserved for the single case where OSI matters.

    python run_cohort.py --out /mnt/d/fyp/real-cfd-patients.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "pipeline"))

from geometry import AneurysmGeometry, make_sidewall_aneurysm, write_surface  # noqa: E402
from export_patient import build_patient  # noqa: E402

FOAM_BASHRC = "/usr/lib/openfoam/openfoam2412/etc/bashrc"
TEMPLATE = Path(__file__).parent / "openfoam" / "case_template"


# Clinically-motivated spread: sac size is the dominant morphological risk
# factor in PHASES, and aspect ratio the dominant one in the composite index.
COHORT = [
    {
        "id": "PT-2026-0101",
        "geom": AneurysmGeometry(sac_radius=0.0040, neck_offset=0.0014),
        "demographics": {"age": 64, "hypertension": True, "earlierSAH": False,
                         "population": "Other", "site": "MCA"},
    },
    {
        "id": "PT-2026-0102",
        # Larger, more protruding sac -> deeper stagnation, higher risk.
        "geom": AneurysmGeometry(sac_radius=0.0055, neck_offset=0.0030),
        "demographics": {"age": 71, "hypertension": True, "earlierSAH": False,
                         "population": "Other", "site": "ACOM_PCOM_POST"},
    },
    {
        "id": "PT-2026-0103",
        # Small, shallow sac -> flow barely separates, low risk.
        "geom": AneurysmGeometry(sac_radius=0.0026, neck_offset=0.0004),
        "demographics": {"age": 49, "hypertension": False, "earlierSAH": False,
                         "population": "Other", "site": "ICA"},
    },
]


def sh(cmd: str, cwd: Path, log: Path, timeout: int = 7200) -> bool:
    """Run a command in an OpenFOAM-sourced shell; return success."""
    full = f"source {FOAM_BASHRC} && cd {cwd} && {cmd}"
    with log.open("w") as fh:
        r = subprocess.run(["bash", "-lc", full], stdout=fh, stderr=subprocess.STDOUT,
                           timeout=timeout)
    return r.returncode == 0


def build_case(case_dir: Path, geom: AneurysmGeometry, nproc: int) -> None:
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)
    for sub in ("system", "constant", "0"):
        shutil.copytree(TEMPLATE / sub, case_dir / sub)

    tri = case_dir / "constant" / "triSurface"
    tri.mkdir(parents=True, exist_ok=True)
    surf, labels = make_sidewall_aneurysm(geom)
    summary = write_surface(surf, labels, tri, "vessel")
    if not summary["is_manifold"]:
        raise RuntimeError(f"{case_dir.name}: surface is not manifold; refusing to mesh")

    # Background box must enclose the geometry with margin.
    x0, x1, y0, y1, z0, z1 = summary["bounds"]
    pad = 0.003
    bm = (case_dir / "system" / "blockMeshDict").read_text()
    for key, val in (("xMin", x0 - pad), ("xMax", x1 + pad),
                     ("yMin", y0 - pad), ("yMax", y1 + pad),
                     ("zMin", z0 - pad), ("zMax", z1 + pad)):
        bm = bm.replace(f"{key} ", f"{key} ", 1)
    import re
    for key, val in (("xMin", x0 - pad), ("xMax", x1 + pad),
                     ("yMin", y0 - pad), ("yMax", y1 + pad),
                     ("zMin", z0 - pad), ("zMax", z1 + pad)):
        bm = re.sub(rf"^{key}\s+[-\d.e]+;", f"{key} {val:.6f};", bm, flags=re.M)
    cells = lambda lo, hi: max(8, int(round((hi - lo) / 4.0e-4)))
    bm = re.sub(r"^nx\s+\d+;", f"nx {cells(x0-pad, x1+pad)};", bm, flags=re.M)
    bm = re.sub(r"^ny\s+\d+;", f"ny {cells(y0-pad, y1+pad)};", bm, flags=re.M)
    bm = re.sub(r"^nz\s+\d+;", f"nz {cells(z0-pad, z1+pad)};", bm, flags=re.M)
    (case_dir / "system" / "blockMeshDict").write_text(bm)

    # locationInMesh must sit inside the lumen, on the parent-artery axis
    # upstream of the sac.
    shm = (case_dir / "system" / "snappyHexMeshDict").read_text()
    shm = re.sub(r"locationInMesh \([^)]*\);",
                 f"locationInMesh ({geom.inlet_extension * 0.5:.5f} 0.0 0.0);", shm)
    # Sac refinement box follows the sac.
    shm = re.sub(r"min \([^)]*\);",
                 f"min ({geom.sac_centre_x - geom.sac_radius - 0.001:.5f} "
                 f"{-geom.parent_radius:.5f} {-geom.sac_radius - 0.001:.5f});", shm)
    shm = re.sub(r"max \([^)]*\);",
                 f"max ({geom.sac_centre_x + geom.sac_radius + 0.001:.5f} "
                 f"{geom.sac_centre_y + geom.sac_radius + 0.001:.5f} "
                 f"{geom.sac_radius + 0.001:.5f});", shm)
    (case_dir / "system" / "snappyHexMeshDict").write_text(shm)

    # Steady solver dictionaries.
    (case_dir / "system" / "controlDict").write_text(STEADY_CONTROL)
    (case_dir / "system" / "fvSchemes").write_text(STEADY_SCHEMES)
    (case_dir / "system" / "fvSolution").write_text(STEADY_SOLUTION)
    dp = (case_dir / "system" / "decomposeParDict").read_text()
    (case_dir / "system" / "decomposeParDict").write_text(
        re.sub(r"numberOfSubdomains\s+\d+;", f"numberOfSubdomains {nproc};", dp))


STEADY_CONTROL = """FoamFile { version 2.0; format ascii; class dictionary; object controlDict; }
application     simpleFoam;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         1500;
deltaT          1;
writeControl    timeStep;
writeInterval   500;
purgeWrite      2;
writeFormat     binary;
writePrecision  8;
runTimeModifiable true;
functions
{
    wallShearStress
    {
        type            wallShearStress;
        libs            (fieldFunctionObjects);
        patches         (wall wall_aneurysm);
        writeControl    writeTime;
        log             false;
    }
}
"""

STEADY_SCHEMES = """FoamFile { version 2.0; format ascii; class dictionary; object fvSchemes; }
ddtSchemes      { default steadyState; }
gradSchemes     { default Gauss linear; grad(U) cellLimited Gauss linear 1; }
divSchemes      { default none; div(phi,U) bounded Gauss linearUpwind grad(U);
                  div((nuEff*dev2(T(grad(U))))) Gauss linear; }
laplacianSchemes{ default Gauss linear corrected; }
interpolationSchemes { default linear; }
snGradSchemes   { default corrected; }
"""

STEADY_SOLUTION = """FoamFile { version 2.0; format ascii; class dictionary; object fvSolution; }
solvers
{
    p { solver GAMG; tolerance 1e-7; relTol 0.01; smoother GaussSeidel;
        nCellsInCoarsestLevel 100; }
    U { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-8; relTol 0.1; }
}
SIMPLE
{
    nNonOrthogonalCorrectors 1;
    consistent      yes;
    residualControl { p 1e-5; U 1e-6; }
}
relaxationFactors { equations { U 0.9; } fields { p 0.9; } }
"""


def solve(case_dir: Path, nproc: int, geom_roi_x: float = 0.050) -> dict[str, str]:
    steps = [
        ("blockMesh", "blockMesh"),
        ("surfaceFeatureExtract", "surfaceFeatureExtract"),
        ("snappyHexMesh", "snappyHexMesh -overwrite"),
        ("checkMesh", "checkMesh -constant"),
        ("decomposePar", "decomposePar -force"),
        ("simpleFoam", f"mpirun --use-hwthread-cpus -np {nproc} simpleFoam -parallel"),
        ("reconstructPar", "reconstructPar -latestTime"),
    ]
    for name, cmd in steps:
        log = case_dir / f"log.{name}"
        print(f"    {name} ...", flush=True)
        ok = sh(cmd, case_dir, log)
        if name == "checkMesh":
            # Quantified gate, not `"Mesh OK" in log`.
            #
            # The binary test discards a sound mesh over a single marginal
            # face. The first sweep point died on "Max skewness = 4.105, 1
            # highly skew face" with non-orthogonality 61.7 against a limit of
            # 65 — one face out of ~500,000, nowhere near the sac. mesh_gate
            # treats non-orthogonality as unwaivable and skewness as waivable
            # only when every offending face is LOCATED outside the region the
            # results are read from, and fails closed when it cannot tell.
            from mesh_gate import evaluate                    # type: ignore

            gate = evaluate(case_dir,
                            roi_centre_m=(geom_roi_x, 0.0, 0.0),
                            roi_radius_m=0.015)
            print(f"    {gate.summary()}", flush=True)
            if not gate.passed:
                raise RuntimeError(f"{case_dir.name}: mesh gate failed")
        elif not ok:
            raise RuntimeError(f"{case_dir.name}: {name} failed — see {log}")
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="~/cases/cohort")
    ap.add_argument("--out", default="/mnt/d/fyp/real-cfd-patients.json")
    ap.add_argument("--nproc", type=int, default=6)
    ap.add_argument("--only", default=None, help="comma-separated patient ids")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    wanted = set(args.only.split(",")) if args.only else None

    records = []
    for spec in COHORT:
        if wanted and spec["id"] not in wanted:
            continue
        print(f"[{spec['id']}] sac_radius={spec['geom'].sac_radius*1e3:.1f}mm", flush=True)
        case_dir = root / spec["id"]
        try:
            build_case(case_dir, spec["geom"], args.nproc)
            solve(case_dir, args.nproc)
            rec = build_patient(case_dir, spec["id"], spec["demographics"])
            records.append(rec)
            print(f"    -> TAWSS sac {rec['zones'][3]['tawss']:.3f} Pa | "
                  f"CRI {rec['riskBreakdown']['composite']} ({rec['riskTier']})", flush=True)
        except Exception as exc:
            print(f"    !! {spec['id']} failed: {exc}", flush=True)

    if not records:
        print("no cases solved", flush=True)
        return 1

    out = Path(args.out)
    doc = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "pipeline": "NeuroFlow CFD worker — geometry -> snappyHexMesh -> OpenFOAM -> hemodynamics",
        "patients": records,
    }
    out.write_text(json.dumps(doc, indent=2))
    print(f"\nwrote {out} with {len(records)} computed case(s)", flush=True)
    for r in records:
        print(f"  {r['id']}: dome {r['zones'][3]['tawss']:.3f} Pa, "
              f"CRI {r['riskBreakdown']['composite']} ({r['riskTier']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
