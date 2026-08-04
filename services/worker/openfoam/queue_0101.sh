#!/usr/bin/env bash
#
# Run PT-2026-0101 pulsatile after PT-2026-0103 has finished AND been finalised.
#
# SEQUENCING, which is the whole reason this is a script rather than a second
# background launch:
#
#   chain_osi.sh waits for `pgrep -x pimpleFoam` to come back empty. If 0101
#   started the moment 0103's solver exited, the chain would wake mid-sleep,
#   see a solver running again, and keep waiting — delaying 0103's real OSI by
#   the seven hours 0101 takes. So this waits for the chain to actually finish
#   its work before starting anything.
#
#   The wait is on a marker in the chain's own log rather than on a pgrep
#   pattern: `pgrep -f chain_osi` risks matching this script's process tree,
#   which is how two earlier waiters deadlocked on each other.
#
# 0101 is 511k cells against 0103's 239k, so it gets 6 ranks (one per physical
# core) rather than 4. Ten ranks on six cores cost a 13x slowdown earlier
# tonight — oversubscribed MPI ranks spin-wait — so nothing else should be
# solving while this runs.
#
#   setsid nohup bash queue_0101.sh > ~/queue_0101.log 2>&1 &

set -o pipefail

CASE="$HOME/cases/cohort/PT-2026-0101"
REPO=/mnt/d/fyp
CHAIN_LOG="$HOME/osi_chain.log"
CHAIN_DONE="Review the numbers above"      # chain_osi.sh's final log line
RUNNER="$HOME/bin/run_pulsatile.snapshot.sh"
FOAM=/usr/lib/openfoam/openfoam2412/etc/bashrc

log() { echo "[$(date +%H:%M:%S)] $*"; }

log "waiting for the 0103 solver to finish…"
while pgrep -x pimpleFoam >/dev/null 2>&1; do
    sleep 60
done
log "solver finished"

log "waiting for chain_osi to finalise 0103…"
for _ in $(seq 1 80); do            # 80 x 30 s = 40 min ceiling
    grep -q "$CHAIN_DONE" "$CHAIN_LOG" 2>/dev/null && break
    sleep 30
done
if grep -q "$CHAIN_DONE" "$CHAIN_LOG" 2>/dev/null; then
    log "0103 finalised"
else
    log "WARNING: chain_osi did not report completion within 40 min; continuing anyway"
fi

# Fresh copy: run_pulsatile.sh may have been edited since the snapshot, and
# editing a script while bash is executing it corrupts the running instance —
# which is exactly how an earlier launch died with a phantom syntax error.
cp "$REPO/services/worker/openfoam/run_pulsatile.sh" "$RUNNER"

log "starting PT-2026-0101 pulsatile on 6 ranks (511k cells, expect ~7 h)"
NPROC=6 MAXCO=3.0 bash "$RUNNER" "$CASE"
RC=$?
log "run_pulsatile rc=$RC"

if [ "$RC" -ne 0 ]; then
    log "FAILED — leaving the dashboard as it is rather than regenerating from a bad solve"
    exit 1
fi

# shellcheck disable=SC1090
set +u; source "$FOAM"; set -u
source "$HOME/.venvs/neuroflow/bin/activate"

LATEST=$(cd "$CASE" && ls -d [0-9]*.[0-9]* 2>/dev/null | sort -g | tail -1)
if ls "$CASE/$LATEST"/*Mean* >/dev/null 2>&1; then
    log "averaged fields present at t=$LATEST — OSI will be real"
else
    log "WARNING: no *Mean fields at t=$LATEST; OSI would stay zero"
fi

log "regenerating the dashboard export"
cd "$REPO/services/worker" || exit 1
python finalize.py \
    --cases "$HOME/cases/cohort/PT-2026-0101" \
            "$HOME/cases/cohort/PT-2026-0102" \
            "$HOME/cases/cohort/PT-2026-0103" \
    2>&1 | grep -viE "pyvistafuture|extract_surface|will change|silence this" | tail -15

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

log "re-rendering the brain view"
cd "$REPO/services/worker/pipeline" && xvfb-run -a python render_brain.py 2>&1 | tail -6

log "done. Review, then deploy:  cd $REPO && vercel --prod --yes"
