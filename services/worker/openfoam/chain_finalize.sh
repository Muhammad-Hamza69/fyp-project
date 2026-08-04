#!/usr/bin/env bash
# Wait for the pulsatile solve, then fold its results into everything.
#
# Runs after chain_pulsatile.sh so the cardiac-cycle result reaches the
# dashboard without anyone having to notice it finished. The pulsatile case is
# the only one that produces a non-zero OSI — a steady solve has no temporal
# variation to measure — so this is what turns the OSI gauge from a structural
# zero into a real number.
#
#   setsid nohup bash chain_finalize.sh > ~/finalize_chain.log 2>&1 &

set -o pipefail

CASE="${1:-$HOME/cases/synthetic01_pulsatile}"
REPO="${2:-/mnt/d/fyp}"
VENV="$HOME/.venvs/neuroflow/bin/activate"

log() { echo "[$(date +%H:%M:%S)] $*"; }

log "waiting for the pulsatile solve…"
# -x on the binary name: `pgrep -f` would match this script's own command line
# and deadlock, which is exactly what happened between two earlier waiters.
while pgrep -x pimpleFoam >/dev/null 2>&1; do
    sleep 60
done
log "solver finished"

# shellcheck disable=SC1090
source "$VENV"

# The cycle-averaged fields are what make OSI meaningful. Verify rather than
# assume: a run that stopped before fieldAverage's timeStart leaves a complete,
# valid-looking case with no averaged data in it, and OSI would silently stay 0.
LATEST=$(cd "$CASE" && ls -d [0-9]*.[0-9]* 2>/dev/null | sort -g | tail -1)
if [[ -z "$LATEST" ]]; then
    log "FATAL: no reconstructed time directory in $CASE — nothing to finalise"
    exit 1
fi
log "latest time: $LATEST"

if ls "$CASE/$LATEST"/*Mean* >/dev/null 2>&1; then
    log "cycle-averaged fields present ($(ls "$CASE/$LATEST" | grep -c Mean) field(s)) — OSI will be real"
else
    log "WARNING: no *Mean fields at $LATEST; OSI will remain zero."
    log "         fieldAverage timeStart = $(grep -oP 'timeStart\s+\K[0-9.]+' "$CASE/system/controlDict" | head -1)"
fi

log "finalising all cases (meshes, JSON, database, reports)"
cd "$REPO/services/worker" || exit 1
python finalize.py \
    --cases "$HOME/cases/cohort/PT-2026-0101" \
            "$HOME/cases/cohort/PT-2026-0102" \
            "$HOME/cases/cohort/PT-2026-0103" \
            "$CASE" \
    2>&1 | grep -viE "pyvistafuture|extract_surface|will change|silence this" | tail -20

log "OSI in the regenerated export:"
python - <<'PY'
import json, pathlib
p = pathlib.Path("/mnt/d/fyp/real-cfd-patients.json")
doc = json.loads(p.read_text())
for rec in doc["patients"]:
    dome = rec["zones"][3]
    print(f"   {rec['id']}  TAWSS {dome['tawss']:.4f} Pa   OSI {dome['osi']:.4f}   "
          f"CRI {rec['riskBreakdown']['composite']} ({rec['riskTier']})")
PY

log "done — redeploy with:  cd $REPO && vercel deploy --prod --yes"
