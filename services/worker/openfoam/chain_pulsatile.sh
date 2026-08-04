#!/usr/bin/env bash
# Wait for the CFD queue to drain, then run the pulsatile cardiac cycle.
#
# The pulsatile solve is the only route to a non-zero OSI: oscillatory shear
# measures directional reversal over the cardiac cycle, and a steady solution
# has none by definition. It needs the machine to itself — sharing six cores
# with the cohort roughly halved throughput when both ran together earlier.
#
# Resumes from the last written time (controlDict has `startFrom latestTime`),
# so an interrupted run continues rather than restarting.
#
# Launch detached so it survives the shell that started it:
#   setsid nohup bash chain_pulsatile.sh > ~/chain.log 2>&1 &

# NOTE: deliberately NOT `set -u`. OpenFOAM's etc/bashrc references variables
# before defining them (WM_PROJECT_DIR among others), so nounset kills the
# script the moment it is sourced. `set -e` is also avoided: this script must
# reach its diagnostic reporting even when a stage fails.
set -o pipefail

CASE="${1:-$HOME/cases/synthetic01_pulsatile}"
NPROC="${2:-6}"
FOAM=/usr/lib/openfoam/openfoam2412/etc/bashrc

log() { echo "[$(date +%H:%M:%S)] $*"; }

log "waiting for the CFD queue to drain…"
# Match SOLVER PROCESSES ONLY, with -x (exact executable name).
#
# An earlier version also did `pgrep -f run_cohort`, which deadlocked: any
# other shell whose command line merely CONTAINED the string "run_cohort" —
# including a separate waiter script watching the same job — matched it, so the
# two waiters each blocked on the other's command line and neither ever
# proceeded. `pgrep -f` matching your own tooling is a recurring hazard; -x on
# the binary name cannot match a shell wrapper.
while pgrep -x simpleFoam >/dev/null 2>&1 \
   || pgrep -x pimpleFoam >/dev/null 2>&1 \
   || pgrep -x snappyHexMesh >/dev/null 2>&1; do
    sleep 30
done
log "queue clear"

# shellcheck disable=SC1090
source "$FOAM"
cd "$CASE" || { log "FATAL: no case at $CASE"; exit 1; }

LATEST=$(ls -d [0-9]*.[0-9]* 2>/dev/null | sort -g | tail -1)
log "resuming from t=${LATEST:-0} toward endTime=$(grep -oP '^endTime\s+\K[0-9.]+' system/controlDict)"

# Re-decompose against whatever time directory exists now; a stale decomposition
# from an earlier run would silently restart the solve from the wrong field.
log "decomposePar"
decomposePar -force -latestTime > log.decomposePar 2>&1 \
  || { log "FATAL: decomposePar failed"; tail -20 log.decomposePar; exit 1; }

# --use-hwthread-cpus: WSL exposes logical CPUs while OpenMPI counts physical
# cores, and otherwise refuses this rank count with "not enough slots".
log "pimpleFoam on ${NPROC} ranks — expect roughly 5 hours"
mpirun --use-hwthread-cpus -np "$NPROC" pimpleFoam -parallel > log.pimpleFoam 2>&1
RC=$?
log "pimpleFoam exited rc=${RC}, last time: $(grep -E '^Time = ' log.pimpleFoam | tail -1)"

if [[ $RC -ne 0 ]]; then
    log "solver failed — see $CASE/log.pimpleFoam"
    grep -A8 "FOAM FATAL" log.pimpleFoam | head -12
    exit $RC
fi

log "reconstructPar"
reconstructPar -latestTime > log.reconstructPar 2>&1

# Cycle-averaged fields are what make OSI meaningful; confirm they exist rather
# than assuming, because a run that stopped before fieldAverage's timeStart
# produces a valid-looking case with no averaged data in it.
FINAL=$(ls -d [0-9]*.[0-9]* 2>/dev/null | sort -g | tail -1)
if ls "$FINAL"/*Mean* >/dev/null 2>&1; then
    log "cycle-averaged fields present in $FINAL: $(ls "$FINAL" | grep -c Mean) field(s)"
else
    log "WARNING: no *Mean fields in $FINAL — OSI will still be zero."
    log "         fieldAverage timeStart is $(grep -oP 'timeStart\s+\K[0-9.]+' system/controlDict | head -1)"
fi

log "done. Next: python services/worker/finalize.py --cases $CASE"
