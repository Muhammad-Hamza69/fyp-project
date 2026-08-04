#!/usr/bin/env bash
#
# Wait for the pulsatile solves, reconstruct them, and fold real OSI into the
# dashboard.
#
# REPLACES chain_finalize.sh, which had a hole: nothing in it — nor in
# finalize.py — ever ran `reconstructPar`. A parallel run leaves its results
# scattered across processor0../processorN and writes NO top-level time
# directory, so the chain would reach its own "no reconstructed time directory"
# check and abort, every time, after waiting hours for the solve. Cases that
# happened to be reconstructed by their own run script masked this.
#
# OSI is the whole point of these runs. A steady solve cannot produce it: OSI
# measures how far the wall shear vector reverses over a cardiac cycle, so with
# one flow state it is identically zero. Every check below exists because a
# missing piece yields exactly that same zero, indistinguishable from a real
# measurement of "no oscillation" unless someone looks.
#
#   setsid nohup bash chain_osi.sh > ~/osi_chain.log 2>&1 &

set -o pipefail

REPO="${REPO:-/mnt/d/fyp}"
VENV="$HOME/.venvs/neuroflow/bin/activate"
FOAM=/usr/lib/openfoam/openfoam2412/etc/bashrc

# Cases to reconstruct once the solvers stop. The cohort case is the one the
# dashboard actually displays; the synthetic case validates the method.
CASES=(
    "$HOME/cases/cohort/PT-2026-0103"
    "$HOME/cases/synthetic01_pulsatile"
)

log() { echo "[$(date +%H:%M:%S)] $*"; }

# shellcheck disable=SC1090
set +u; source "$FOAM"; set -u

log "waiting for pulsatile solves to finish…"
# -x on the binary name. `pgrep -f pimpleFoam` matches this script's own command
# line and waits on itself forever — a deadlock two earlier waiters hit.
while pgrep -x pimpleFoam >/dev/null 2>&1; do
    sleep 60
done
log "all solvers finished"

# shellcheck disable=SC1090
source "$VENV"

for CASE in "${CASES[@]}"; do
    [ -d "$CASE" ] || { log "skip (missing): $CASE"; continue; }
    NAME=$(basename "$CASE")

    if ! ls -d "$CASE"/processor0/[0-9]* >/dev/null 2>&1; then
        log "$NAME: no processor data — nothing to reconstruct"
        continue
    fi

    log "$NAME: reconstructing"
    ( cd "$CASE" && reconstructPar -latestTime > log.reconstructPar 2>&1 ) \
        || { log "$NAME: reconstructPar FAILED — see log.reconstructPar"; continue; }

    LATEST=$(cd "$CASE" && ls -d [0-9]*.[0-9]* 2>/dev/null | sort -g | tail -1)
    if [ -z "$LATEST" ]; then
        log "$NAME: still no reconstructed time directory"
        continue
    fi

    # The averaged fields are what make OSI meaningful. A run that stopped
    # before fieldAverage's timeStart leaves a complete, valid-looking case
    # containing no averaged data, and OSI stays 0 with nothing to show why.
    if ls "$CASE/$LATEST"/*Mean* >/dev/null 2>&1; then
        N=$(ls "$CASE/$LATEST" | grep -c Mean)
        log "$NAME: t=$LATEST, $N averaged field(s) — OSI will be real"
    else
        log "$NAME: WARNING t=$LATEST has NO *Mean fields; OSI stays zero"
        log "$NAME: fieldAverage timeStart = $(grep -oP 'timeStart\s+\K[0-9.]+' "$CASE/system/controlDict" | head -1)"
    fi
done

log "regenerating dashboard export"
cd "$REPO/services/worker" || exit 1
python finalize.py \
    --cases "$HOME/cases/cohort/PT-2026-0101" \
            "$HOME/cases/cohort/PT-2026-0102" \
            "$HOME/cases/cohort/PT-2026-0103" \
    2>&1 | grep -viE "pyvistafuture|extract_surface|will change|silence this" | tail -20

log "OSI / ECAP in the regenerated export:"
python - <<'PY'
import json, pathlib
doc = json.loads(pathlib.Path("/mnt/d/fyp/real-cfd-patients.json").read_text())
for rec in doc["patients"]:
    dome = next(z for z in rec["zones"] if "Dome" in z["name"])
    h = rec.get("hemodynamics", {})
    print(f"   {rec['id']}  TAWSS {dome['tawss']:.4f} Pa   OSI {dome['osi']:.4f}   "
          f"ECAP {h.get('ecap', 0):.4f}   transient={h.get('transient')}   "
          f"CRI {rec['riskBreakdown']['composite']} ({rec['riskTier']})")
PY

log "re-rendering the brain view with the new values"
cd "$REPO/services/worker/pipeline" \
    && xvfb-run -a python render_brain.py 2>&1 | tail -6

log "done. Review the numbers above, then deploy:"
log "   cd $REPO && vercel --prod --yes"
