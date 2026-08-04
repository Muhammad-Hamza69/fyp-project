"""
Celery task definitions — the asynchronous execution layer.

A CFD solve takes hours, so it cannot run inside an HTTP request. This module
orchestrates the full pipeline as a Celery task and — importantly — writes
progress to the `job_stages` TABLE at every stage boundary rather than only
emitting it to a websocket.

That distinction matters: a browser opened three hours into a solve, or reloaded
after a laptop sleep, must still render accurate progress. A stream-only design
shows such a client an empty log and no state.

Queues are separated by resource profile so a 3-hour solve cannot block a
2-second metadata extraction:

    cpu      short I/O- and CPU-bound stages          concurrency 2
    cfd      the solver                               concurrency 1 (solo)
    ai       feature extraction + inference           concurrency 2
    reports  PDF rendering                            concurrency 2
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from celery import Celery

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE / "pipeline"))
sys.path.insert(0, str(_HERE.parent / "api"))

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

celery_app = Celery("neuroflow", broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    worker_prefetch_multiplier=1,      # a long task must not hoard the queue
    task_acks_late=True,               # redeliver if a worker dies mid-solve
    # Upstash and other hosted Redis bill per command; the default aggressive
    # BRPOP polling across many queues burns a free tier in days.
    broker_transport_options={"polling_interval": 5.0},
    task_routes={
        "neuroflow.solve_case": {"queue": "cfd"},
        "neuroflow.ingest_study": {"queue": "cpu"},
        "neuroflow.segment_study": {"queue": "cpu"},
        "neuroflow.predict_risk": {"queue": "ai"},
        "neuroflow.build_report": {"queue": "reports"},
    },
)


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #

def _set_stage(run_id: str, stage_name: str, state: str,
               progress: float = 0.0, message: str | None = None,
               metrics: dict[str, Any] | None = None) -> None:
    """Write durable stage progress. Never raises — progress must not fail a job."""
    try:
        from db import get_session
        from models import JobStage, JobState, Run

        s = get_session()
        try:
            stage = JobState(stage_name)
            row = (s.query(JobStage)
                     .filter(JobStage.run_id == run_id, JobStage.stage == stage)
                     .one_or_none())
            if row is None:
                row = JobStage(run_id=run_id, stage=stage)
                s.add(row)
            row.state = state
            row.progress = progress
            if message:
                row.message = message
            if metrics:
                row.metrics = metrics
            now = datetime.now(timezone.utc)
            if state == "running" and row.started_at is None:
                row.started_at = now
            if state in ("done", "failed"):
                row.ended_at = now

            run = s.get(Run, run_id)
            if run is not None and state == "running":
                run.state = stage
            s.commit()
        finally:
            s.close()
    except Exception:  # noqa: BLE001
        # Progress reporting is strictly best-effort; a database blip must not
        # kill a three-hour solve.
        pass


def _finish(run_id: str, ok: bool, error: str | None = None) -> None:
    try:
        from db import get_session
        from models import JobState, Run

        s = get_session()
        try:
            run = s.get(Run, run_id)
            if run:
                run.state = JobState.SUCCEEDED if ok else JobState.FAILED
                run.error = error
                run.ended_at = datetime.now(timezone.utc)
                s.commit()
        finally:
            s.close()
    except Exception:  # noqa: BLE001
        pass


def _cancelled(run_id: str) -> bool:
    """Checked at every stage boundary so cancel actually cancels."""
    try:
        from db import get_session
        from models import JobState, Run

        s = get_session()
        try:
            run = s.get(Run, run_id)
            return bool(run and run.state == JobState.CANCELLING)
        finally:
            s.close()
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #

@celery_app.task(name="neuroflow.ingest_study", bind=True)
def ingest_study(self, run_id: str, dicom_path: str) -> dict[str, Any]:
    """Validate DICOM, extract metadata, assess image quality."""
    from dataclasses import asdict
    from imaging import assess_quality, read_volume, validate_dicom

    try:
        _set_stage(run_id, "VALIDATING", "running", 0.1, "reading DICOM")
        v = validate_dicom(Path(dicom_path))
        if not v.valid:
            _set_stage(run_id, "VALIDATING", "failed", 1.0, "; ".join(v.reasons))
            _finish(run_id, False, "; ".join(v.reasons))
            return {"ok": False, "reasons": v.reasons}
        _set_stage(run_id, "VALIDATING", "done", 1.0,
                   f"{v.modality} {v.n_slices} slices", asdict(v))

        _set_stage(run_id, "PREPROCESSING", "running", 0.3, "assessing quality")
        q = assess_quality(read_volume(Path(dicom_path)))
        _set_stage(run_id, "PREPROCESSING", "done", 1.0,
                   f"quality {q.score:.2f}", asdict(q))
        return {"ok": True, "validation": asdict(v), "quality": asdict(q)}
    except Exception as exc:  # noqa: BLE001
        _set_stage(run_id, "VALIDATING", "failed", 1.0, str(exc))
        _finish(run_id, False, traceback.format_exc(limit=3))
        raise


@celery_app.task(name="neuroflow.segment_study", bind=True)
def segment_study(self, run_id: str, dicom_path: str, out_dir: str,
                  backend: str = "traditional") -> dict[str, Any]:
    """Preprocess, segment vessels, reconstruct a surface."""
    from dataclasses import asdict
    import SimpleITK as sitk
    from imaging import (get_backend, mask_to_surface, preprocess,
                         read_volume, segmentation_metrics)

    try:
        if _cancelled(run_id):
            return {"ok": False, "cancelled": True}
        out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)

        _set_stage(run_id, "SEGMENTING", "running", 0.2, f"backend={backend}")
        img = preprocess(read_volume(Path(dicom_path)))
        mask = get_backend(backend).segment(img)
        sitk.WriteImage(mask, str(out / "mask.nii.gz"))

        m = segmentation_metrics(mask)
        _set_stage(run_id, "SEGMENTING", "done", 1.0,
                   f"{m.voxel_count} voxels, {m.n_components} component(s)", asdict(m))

        _set_stage(run_id, "RECONSTRUCTING", "running", 0.6, "marching cubes")
        surf = mask_to_surface(mask, out / "vessel.stl")
        _set_stage(run_id, "RECONSTRUCTING", "done", 1.0,
                   f"{surf['n_cells']} triangles", surf)
        return {"ok": True, "segmentation": asdict(m), "surface": surf}
    except Exception as exc:  # noqa: BLE001
        _set_stage(run_id, "SEGMENTING", "failed", 1.0, str(exc))
        _finish(run_id, False, traceback.format_exc(limit=3))
        raise


@celery_app.task(name="neuroflow.solve_case", bind=True)
def solve_case(self, run_id: str, case_dir: str, nproc: int = 6) -> dict[str, Any]:
    """Mesh, solve and post-process. The long one — hours, not seconds."""
    import subprocess
    from hemodynamics import analyse

    foam = "/usr/lib/openfoam/openfoam2412/etc/bashrc"
    case = Path(case_dir).expanduser()

    def run(cmd: str, log: str) -> bool:
        with (case / log).open("w") as fh:
            return subprocess.run(
                ["bash", "-lc", f"source {foam} && cd {case} && {cmd}"],
                stdout=fh, stderr=subprocess.STDOUT,
            ).returncode == 0

    try:
        for stage, cmd, log in (
            ("MESHING", "blockMesh && surfaceFeatureExtract && snappyHexMesh -overwrite", "log.mesh"),
            ("MESH_QC", "checkMesh -constant", "log.checkMesh"),
        ):
            if _cancelled(run_id):
                _finish(run_id, False, "cancelled"); return {"ok": False, "cancelled": True}
            _set_stage(run_id, stage, "running", 0.3, cmd.split()[0])
            run(cmd, log)
            if stage == "MESH_QC":
                text = (case / log).read_text(errors="ignore")
                if "Mesh OK" not in text:
                    _set_stage(run_id, stage, "failed", 1.0, "checkMesh reported errors")
                    _finish(run_id, False, "mesh quality check failed")
                    return {"ok": False, "error": "checkMesh failed"}
            _set_stage(run_id, stage, "done", 1.0)

        if _cancelled(run_id):
            _finish(run_id, False, "cancelled"); return {"ok": False, "cancelled": True}

        _set_stage(run_id, "SOLVING", "running", 0.1, f"pimpleFoam on {nproc} ranks")
        run("decomposePar -force", "log.decomposePar")
        # --use-hwthread-cpus: WSL exposes logical CPUs but OpenMPI counts
        # physical cores and otherwise refuses the rank count.
        run(f"mpirun --use-hwthread-cpus -np {nproc} pimpleFoam -parallel", "log.pimpleFoam")
        run("reconstructPar -latestTime", "log.reconstructPar")
        _set_stage(run_id, "SOLVING", "done", 1.0)

        _set_stage(run_id, "POSTPROCESSING", "running", 0.5, "extracting wall shear stress")
        hemo = analyse(case)
        _set_stage(run_id, "POSTPROCESSING", "done", 1.0,
                   f"sac TAWSS {hemo['zones'][1]['tawss']:.3f} Pa")
        return {"ok": True, "hemodynamics": hemo}
    except Exception as exc:  # noqa: BLE001
        _set_stage(run_id, "SOLVING", "failed", 1.0, str(exc))
        _finish(run_id, False, traceback.format_exc(limit=3))
        raise


@celery_app.task(name="neuroflow.predict_risk", bind=True)
def predict_risk(self, run_id: str, hemo: dict, morphology: dict,
                 demographics: dict) -> dict[str, Any]:
    from risk_model import extract_features, predict

    _set_stage(run_id, "FEATURE_EXTRACTION", "running", 0.5)
    feats = extract_features(hemo, morphology, demographics)
    _set_stage(run_id, "FEATURE_EXTRACTION", "done", 1.0, metrics=feats.values)

    _set_stage(run_id, "RISK_PREDICTION", "running", 0.5)
    out = predict(feats, _HERE / "models")
    _set_stage(run_id, "RISK_PREDICTION", "done", 1.0,
               f"p={out['probability']:.3f} ({out['risk_category']})")
    return out


@celery_app.task(name="neuroflow.build_report", bind=True)
def build_report(self, run_id: str, record: dict, out_pdf: str,
                 ai: dict | None = None) -> dict[str, Any]:
    from report import generate_report

    _set_stage(run_id, "REPORTING", "running", 0.5, "rendering PDF")
    res = generate_report(record, Path(out_pdf), ai)
    _set_stage(run_id, "REPORTING", "done", 1.0, f"{res['bytes']} bytes")
    _finish(run_id, True)
    return res
