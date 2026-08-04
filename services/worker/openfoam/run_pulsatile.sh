#!/usr/bin/env bash
#
# Restart an already-solved steady case as a pulsatile one, to obtain OSI.
#
# WHY THIS EXISTS
# A steady solve cannot produce OSI. OSI measures how far the wall shear vector
# reverses over a cardiac cycle; with no cycle it is identically zero, and ECAP
# (= OSI/TAWSS) is zero with it. Reporting those zeros as measurements is wrong
# — they are "not computed", not "no oscillation". The only way to compute them
# is to run the cycle, which is what this does.
#
# It warm-starts from the case's existing converged steady solution, so the
# start-up transient is mostly gone and ONE cycle is usable rather than two or
# three.
#
# THREE BUGS THIS EXISTS TO NOT REPEAT — all of them silent in the first attempt
# at ~/cases/pulsatile_coarse, which reported success and produced nothing:
#
#   1. `foamFormatConvert` honours controlDict's writeFormat, which is `binary`.
#      The 0/U it produced was binary, so the Python that rewrites the inlet
#      died on UnicodeDecodeError. With `pipefail` but no `set -e` the script
#      sailed past it and solved with the STEADY (constant) inlet — a pulsatile
#      run with no pulse.
#
#   2. The steady warm start leaves an iteration directory behind (e.g. `1500`).
#      With `startFrom latestTime` the solver starts at t=1500, which is already
#      past `endTime 0.90`, so it printed "Starting time loop" then "End" and
#      exited having advanced zero timesteps. Exit code 0.
#
#   3. Nothing verified that any of it worked. The run "succeeded" three times
#      over while producing no averaged fields at all.
#
# So every step below is checked, and the script stops at the first failure.
#
#   NPROC=4 bash run_pulsatile.sh ~/cases/cohort/PT-2026-0103
#
set -o pipefail

CASE="${1:?usage: run_pulsatile.sh <case-dir>}"
NPROC="${NPROC:-4}"
CYCLE="${CYCLE:-0.90}"
# PIMPLE is implicit, so it tolerates Courant numbers well above 1. The case
# template ships maxCo 1.0, which is safe but forces ~3x more timesteps than
# necessary: on the 239k-cell cohort case that is the difference between a
# ~17 hour cycle and a ~6 hour one. 3.0 is what the validated fine run uses.
MAXCO="${MAXCO:-3.0}"
# Warm-started from a converged steady field, so averaging can begin early in
# the cycle rather than after a full settling cycle. 0.10 keeps 89% of it.
AVG_START="${AVG_START:-0.10}"
FOAM=/usr/lib/openfoam/openfoam2412/etc/bashrc

log()  { echo "[$(date +%H:%M:%S)] $*"; }
die()  { echo "[$(date +%H:%M:%S)] FATAL: $*" >&2; exit 1; }

# shellcheck disable=SC1090
set +u; source "$FOAM"; set -u

cd "$CASE" || die "no such case: $CASE"
log "case: $CASE  ranks: $NPROC"

# --- 1. find the converged steady solution -------------------------------
STEADY=$(ls -d [0-9]* 2>/dev/null | grep -vx 0 | sort -g | tail -1)

if [ -n "$STEADY" ]; then
    [ -f "$STEADY/U" ] || die "$STEADY/U missing — steady run did not write fields"
    log "warm-starting from steady iteration $STEADY"
    # --- 2. seed t=0 with it ---------------------------------------------
    cp "$STEADY/U" "$STEADY/p" 0/ || die "could not seed 0/ from $STEADY"
elif grep -q "internalField[[:space:]]*nonuniform" 0/U 2>/dev/null; then
    # Already seeded by an earlier attempt. Step 5 deletes the steady iteration
    # directories once 0/ carries their solution, so a run that fails AFTER
    # that point leaves no steady directory to find — and re-running would
    # otherwise abort claiming there was never a warm start, when in fact the
    # warm-started field is sitting right there in 0/.
    log "no steady dir, but 0/ already holds a warm-started field — reusing it"
else
    die "no steady solution to warm-start from, and 0/U is uniform"
fi

# --- 3. pulsatile numerics ----------------------------------------------
# ALL THREE system files come from the transient template, controlDict included.
#
# Patching the steady controlDict in place is not enough, and failing to notice
# that cost a run: the steady dict carries `deltaT 1` (one second per SIMPLE
# iteration), so the first transient step jumped to t=1, overshot endTime 0.90,
# and the solver stopped after a single step at Courant 6840. It also has no
# fieldAverage function objects at all, so even a correct time loop would have
# produced no averaged fields and therefore no OSI — the precise failure this
# script exists to prevent.
TPL=/mnt/d/fyp/services/worker/openfoam/case_template
for f in controlDict fvSchemes fvSolution; do
    [ -f "$TPL/system/$f" ] || die "$TPL/system/$f missing"
    cp "$TPL/system/$f" system/ || die "could not install $f"
done
grep -q "backward\|CrankNicolson\|Euler" system/fvSchemes \
    || die "fvSchemes has no transient ddt scheme — this would not be a cycle"

# --- 4. rewrite the inlet as a cardiac waveform --------------------------
# Convert time 0 to ASCII FIRST. foamFormatConvert follows controlDict's
# writeFormat, so it must be flipped to ascii or it writes binary and the
# rewrite below cannot read it (bug 1).
sed -i 's/^writeFormat.*/writeFormat     ascii;/' system/controlDict
foamFormatConvert -time 0 > log.formatConvert 2>&1 \
    || die "foamFormatConvert failed — see log.formatConvert"

python3 - <<'PY' || die "inlet rewrite failed"
import pathlib, re, sys

# ICA waveform, T = 0.9 s, mean ~4.6 mL/s (Ford 2005 / Hoi 2010).
INLET = """    inlet
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

p = pathlib.Path("0/U")
try:
    t = p.read_text()
except UnicodeDecodeError:
    sys.exit("0/U is still binary — foamFormatConvert did not convert it")

t, n = re.subn(r"    inlet\s*\n    \{.*?\n    \}", INLET, t, count=1, flags=re.S)
if n != 1:
    sys.exit("inlet block not found in 0/U")
p.write_text(t)

# Prove it landed rather than trusting the substitution count.
if "flowRateInletVelocity" not in p.read_text():
    sys.exit("inlet rewrite did not persist")
print("pulsatile inlet applied")
PY

grep -q "flowRateInletVelocity" 0/U || die "0/U has no pulsatile inlet after rewrite"
sed -i 's/^writeFormat.*/writeFormat     binary;/' system/controlDict

# --- 5. time controls ----------------------------------------------------
# startFrom MUST be startTime, not latestTime: the steady iteration directory
# left behind is numerically larger than endTime, so latestTime would start the
# run past its own end and exit having done nothing (bug 2).
python3 - "$CYCLE" "$AVG_START" "$MAXCO" <<'PY' || die "controlDict edit failed"
import pathlib, re, sys
cycle, avg, maxco = sys.argv[1], sys.argv[2], sys.argv[3]
c = pathlib.Path("system/controlDict"); s = c.read_text()
s = re.sub(r"^application\s+\S+;", "application     pimpleFoam;", s, flags=re.M)
s = re.sub(r"^startFrom\s+\S+;",   "startFrom       startTime;",  s, flags=re.M)
s = re.sub(r"^startTime\s+\S+;",   "startTime       0;",          s, flags=re.M)
s = re.sub(r"^endTime\s+\S+;",     f"endTime         {cycle};",   s, flags=re.M)
s = re.sub(r"^maxCo\s+\S+;",       f"maxCo           {maxco};",   s, flags=re.M)
s = re.sub(r"^(\s*)timeStart\s+\S+;", rf"\1timeStart       {avg};", s, flags=re.M)
c.write_text(s)
for k, v in (("application","pimpleFoam"), ("startFrom","startTime"), ("endTime",cycle)):
    if not re.search(rf"^{k}\s+{re.escape(v)};", s, flags=re.M):
        sys.exit(f"{k} was not set to {v}")

# The two properties that decide whether this is a cardiac cycle or one
# enormous step. A steady dict's deltaT of 1 s overshoots a 0.9 s cycle on the
# very first iteration and the solver stops having computed nothing.
m = re.search(r"^deltaT\s+([0-9.eE+-]+);", s, flags=re.M)
if not m or float(m.group(1)) > 0.01:
    sys.exit(f"deltaT is {m.group(1) if m else 'absent'} — far too large for a "
             f"{cycle}s cycle; the steady controlDict was not replaced")
if not re.search(r"^adjustTimeStep\s+yes;", s, flags=re.M):
    sys.exit("adjustTimeStep is not on — a fixed 1e-5 step would take days")

# Without these there is no OSI, however well the flow itself solves.
for fo in ("wallShearStress", "fieldAverage"):
    if fo not in s:
        sys.exit(f"controlDict has no {fo} function object — no averaged fields, "
                 "so OSI would come out zero exactly as it does now")
print(f"pimpleFoam, t=0..{cycle}, averaging from {avg}, deltaT {m.group(1)}, maxCo {maxco}")
PY

# Remove the steady iteration directories now that 0/ carries their solution.
# Leaving them also makes `reconstructPar -latestTime` and the final glob pick
# the wrong directory.
for d in $(ls -d [0-9]* 2>/dev/null | grep -vx 0); do rm -rf "$d"; done
log "steady iteration dirs cleared; latest time is now $(ls -d [0-9]* | sort -g | tail -1)"

# --- 6. solve ------------------------------------------------------------
sed -i "s/^numberOfSubdomains.*/numberOfSubdomains ${NPROC};/" system/decomposeParDict
decomposePar -force > log.decomposePar 2>&1 || die "decomposePar failed"

log "pimpleFoam on $NPROC ranks — one cardiac cycle (this takes hours)"
mpirun --use-hwthread-cpus -np "$NPROC" pimpleFoam -parallel > log.pimpleFoam 2>&1
RC=$?

# --- 7. verify it actually ran ------------------------------------------
NSTEPS=$(grep -cE "^Time = " log.pimpleFoam)
LAST=$(grep -E "^Time = " log.pimpleFoam | tail -1)
[ "$NSTEPS" -gt 0 ] || die "solver advanced ZERO timesteps (rc=$RC) — check startFrom/endTime"
# "at least one step" is too weak a test: the run that prompted this took
# exactly one step of 1 s, satisfied that check, and produced nothing. A real
# cycle at maxCo 1.0 is tens of thousands of steps.
[ "$NSTEPS" -gt 100 ] || die "only $NSTEPS timestep(s) for a ${CYCLE}s cycle (rc=$RC, $LAST) \
— deltaT is almost certainly wrong"
log "rc=$RC, $NSTEPS timesteps, last $LAST"

reconstructPar -latestTime > log.reconstructPar 2>&1 || die "reconstructPar failed"

FINAL=$(ls -d [0-9]*.[0-9]* 2>/dev/null | sort -g | tail -1)
[ -n "$FINAL" ] || die "no reconstructed time directory"
ls "$FINAL"/*Mean* >/dev/null 2>&1 \
    || die "no *Mean fields in $FINAL — OSI would still be zero, which is the "\
"exact failure this script exists to prevent"

log "cycle-averaged fields present in $FINAL — OSI is real"
log "done"
