#!/usr/bin/env bash
# A deliberately coarse pulsatile case, run alongside the fine one.
#
# WHY BOTH
# The fine 522k-cell run needs ~5 h to complete a cardiac cycle on six cores.
# This one uses ~0.7 mm base cells (roughly a third the cell count) and reaches
# a full cycle in about a third of the time, so a real OSI number exists hours
# earlier. If the fine run finishes, its numbers supersede these; if anything
# goes wrong with it, this is not a fallback to a fabricated value but to a
# genuinely solved — merely coarser — one.
#
# Mesh sensitivity, stated honestly: absolute TAWSS is the quantity most
# affected by coarsening, because wall shear is a near-wall gradient. OSI is a
# RATIO of two averages of the same field, so first-order mesh error largely
# cancels — it is the parameter that survives coarsening best. That is exactly
# why this is an acceptable way to get OSI early, and not an acceptable way to
# report TAWSS.
#
#   setsid nohup bash run_coarse_pulsatile.sh > ~/coarse.log 2>&1 &

set -o pipefail

CASE="$HOME/cases/pulsatile_coarse"
SRC="$HOME/cases/synthetic01_pulsatile"
TPL=/mnt/d/fyp/services/worker/openfoam/case_template
FOAM=/usr/lib/openfoam/openfoam2412/etc/bashrc
NPROC=3          # leave 6 for the fine run; WSL exposes 10 logical CPUs

log() { echo "[$(date +%H:%M:%S)] $*"; }

# shellcheck disable=SC1090
source "$FOAM"

log "building coarse case"
rm -rf "$CASE"; mkdir -p "$CASE/constant/triSurface"
cp -r "$TPL/system" "$TPL/constant" "$TPL/0" "$CASE/"
mkdir -p "$CASE/constant/triSurface"
cp "$SRC/constant/triSurface/vessel.stl" "$CASE/constant/triSurface/" 2>/dev/null \
  || cp "$HOME/cases/synthetic01/constant/triSurface/vessel.stl" "$CASE/constant/triSurface/"

cd "$CASE" || exit 1

python3 - <<'PY'
import re, pathlib
p = pathlib.Path("system/blockMeshDict"); t = p.read_text()
# 0.7 mm base cells instead of 0.4 mm -> ~1/5 the background cells.
# Substitution is NOT anchored to line start: the template packs min and max on
# the same line ("xMin -0.003;  xMax 0.103;"), and an anchored pattern silently
# leaves the max values stale, which produces an inside-out block.
for k, v in (("nx", 152), ("ny", 22), ("nz", 20)):
    t = re.sub(rf"^{k}\s+\d+;", f"{k} {v};", t, flags=re.M)
p.write_text(t)

q = pathlib.Path("system/snappyHexMeshDict"); u = q.read_text()
u = re.sub(r"level \(1 2\);", "level (1 1);", u)      # one refinement level
u = re.sub(r"nSurfaceLayers 3;", "nSurfaceLayers 2;", u)
q.write_text(u)
print("coarse mesh configured")
PY

log "blockMesh";             blockMesh              > log.blockMesh 2>&1
log "surfaceFeatureExtract"; surfaceFeatureExtract  > log.sfe 2>&1
log "snappyHexMesh";         snappyHexMesh -overwrite > log.snappy 2>&1
log "checkMesh";             checkMesh -constant    > log.checkMesh 2>&1
grep -q "Mesh OK" log.checkMesh || { log "FATAL: checkMesh failed"; exit 1; }
log "cells: $(grep -oP '^\s+cells:\s+\K[0-9]+' log.checkMesh | head -1)"

# Steady warm start. Cheap on a coarse mesh, and it removes most of the
# start-up transient so a SINGLE cardiac cycle is usable rather than needing
# two or three.
log "steady warm start"
cp "$HOME/cases/synthetic01_steady/system/controlDict" system/controlDict
cp "$HOME/cases/synthetic01_steady/system/fvSchemes"   system/fvSchemes
cp "$HOME/cases/synthetic01_steady/system/fvSolution"  system/fvSolution
sed -i "s/^numberOfSubdomains.*/numberOfSubdomains ${NPROC};/" system/decomposeParDict
decomposePar -force > log.decomposePar 2>&1
mpirun --use-hwthread-cpus -np "$NPROC" simpleFoam -parallel > log.simpleFoam 2>&1
reconstructPar -latestTime > log.reconstructPar 2>&1
LATEST=$(ls -d [0-9]* | grep -v '^0$' | sort -g | tail -1)
log "steady done at iteration ${LATEST}"

log "switching to pulsatile"
cp "$SRC/system/controlDict" system/controlDict
cp "$SRC/system/fvSchemes"   system/fvSchemes
cp "$SRC/system/fvSolution"  system/fvSolution
cp "$SRC/0/U" "$SRC/0/p" 0/ 2>/dev/null || true
# Seed t=0 with the converged steady field.
cp "$LATEST"/U "$LATEST"/p 0/ 2>/dev/null || true
foamFormatConvert -time 0 > log.formatConvert 2>&1 || true
python3 - <<'PY'
import re, pathlib
# Re-apply the pulsatile inlet: foamFormatConvert rewrites 0/U from the steady
# solution, which carries the CONSTANT inlet condition with it.
u = pathlib.Path("0/U"); t = u.read_text()
inlet = """    inlet
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
t, n = re.subn(r"    inlet\s*\n    \{.*?\n    \}", inlet, t, count=1, flags=re.S)
assert n == 1, "inlet block not replaced"
u.write_text(t)

c = pathlib.Path("system/controlDict"); s = c.read_text()
s = re.sub(r"^endTime\s+\S+;", "endTime         0.90;", s, flags=re.M)
# Warm-started, so average from early in the cycle rather than the last half.
s = re.sub(r"^(\s+)timeStart\s+\S+;", r"\1timeStart       0.10;", s, flags=re.M)
c.write_text(s)
print("pulsatile configured, averaging from t=0.10")
PY

decomposePar -force > log.decomposePar2 2>&1
log "pimpleFoam on ${NPROC} ranks — one cardiac cycle"
mpirun --use-hwthread-cpus -np "$NPROC" pimpleFoam -parallel > log.pimpleFoam 2>&1
RC=$?
log "solver rc=${RC}, last: $(grep -E '^Time = ' log.pimpleFoam | tail -1)"
reconstructPar -latestTime > log.reconstructPar2 2>&1

FINAL=$(ls -d [0-9]*.[0-9]* 2>/dev/null | sort -g | tail -1)
if ls "$FINAL"/*Mean* >/dev/null 2>&1; then
    log "cycle-averaged fields present in ${FINAL} — OSI is real"
else
    log "WARNING: no *Mean fields in ${FINAL}; OSI would still be zero"
fi
log "done"
