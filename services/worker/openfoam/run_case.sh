#!/usr/bin/env bash
# Run one OpenFOAM case end-to-end.
#   usage: run_case.sh <case-dir> [nproc]
set -euo pipefail

CASE="${1:?usage: run_case.sh <case-dir> [nproc]}"
NPROC="${2:-6}"

source /usr/lib/openfoam/openfoam2412/etc/bashrc
cd "$CASE"

# WSL2 exposes logical CPUs, but OpenMPI counts physical CORES when deciding how
# many slots exist. With `processors=10` in .wslconfig it sees 5 cores and
# refuses `-np 6` with a "not enough slots" error. --use-hwthread-cpus makes it
# count hardware threads instead.
#
# We still keep NPROC at the PHYSICAL core count (6 on the i7-8850H): OpenFOAM is
# memory-bandwidth bound, so running on hyperthreads adds contention, not speed.
MPI_OPTS="--use-hwthread-cpus"

log() { echo "[$(date +%H:%M:%S)] $*"; }

if [[ ! -d constant/polyMesh ]]; then
  log "blockMesh";              blockMesh              > log.blockMesh 2>&1
  log "surfaceFeatureExtract";  surfaceFeatureExtract  > log.surfaceFeatureExtract 2>&1
  log "snappyHexMesh";          snappyHexMesh -overwrite > log.snappyHexMesh 2>&1
fi

log "checkMesh"
checkMesh -constant > log.checkMesh 2>&1
grep -q "Mesh OK" log.checkMesh || { echo "FATAL: checkMesh failed"; tail -30 log.checkMesh; exit 1; }

log "decomposePar (${NPROC})"
decomposePar -force > log.decomposePar 2>&1

log "pimpleFoam on ${NPROC} ranks"
mpirun $MPI_OPTS -np "$NPROC" pimpleFoam -parallel > log.pimpleFoam 2>&1

log "reconstructPar"
reconstructPar -latestTime > log.reconstructPar 2>&1

log "done"
