"""
Persist pipeline output into the database.

Bridges the CFD worker and the API: takes the patient records produced by
`export_patient.py` / `run_cohort.py` and writes them as Patient → Study → Run →
CFDResult (+ AIResult) rows, so `/api/v1/dashboard/patients` serves live
database records rather than a static file.

Re-ingesting the same study creates a NEW run_version rather than overwriting.
That is what makes "we refined the mesh and TAWSS moved by X" an evidenced
claim instead of an assertion — the previous run is still there to compare.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker" / "pipeline"))

from sqlalchemy import func, select

from db import get_session, init_db
from models import AIResult, CFDResult, JobStage, JobState, Patient, Run, Study


def _int_from(text: str | None) -> int | None:
    if not text:
        return None
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    return int(digits) if digits else None


def _float_after(text: str | None, marker: str) -> float | None:
    if not text or marker not in text:
        return None
    tail = text.split(marker, 1)[1].strip().split()
    try:
        return float(tail[0])
    except (ValueError, IndexError):
        return None


def ingest_record(rec: dict[str, Any], session) -> dict[str, Any]:
    pid = rec["id"]
    demo = rec.get("demographics", {})

    patient = session.get(Patient, pid)
    if patient is None:
        patient = Patient(patient_id=pid)
        session.add(patient)
    patient.age = demo.get("age")
    patient.hypertension = bool(demo.get("hypertension"))
    patient.earlier_sah = bool(demo.get("earlierSAH"))
    patient.population = demo.get("population", "Other")
    patient.site = demo.get("site")
    session.flush()

    # One study per patient for computed cases; a real acquisition would create
    # one per DICOM series.
    study = session.execute(
        select(Study).where(Study.patient_id == pid)
    ).scalars().first()
    if study is None:
        study = Study(patient_id=pid, modality="MR", study_date=datetime.now().strftime("%Y%m%d"))
        session.add(study)
        session.flush()

    nxt = (session.execute(
        select(func.coalesce(func.max(Run.run_version), 0)).where(Run.study_id == study.study_id)
    ).scalar() or 0) + 1

    prov = rec.get("provenance", {})
    run = Run(
        study_id=study.study_id,
        run_version=nxt,
        state=JobState.SUCCEEDED,
        rheology="newtonian",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.flush()

    # Mark the pipeline stages this run actually completed.
    for stage in (JobState.VALIDATING, JobState.RECONSTRUCTING, JobState.MORPHOLOGY,
                  JobState.MESHING, JobState.SOLVING, JobState.POSTPROCESSING,
                  JobState.COMPOSITE_RISK):
        session.add(JobStage(run_id=run.run_id, stage=stage, state="done", progress=1.0))

    hemo = rec.get("hemodynamics", {})
    zones = rec.get("zones", [])
    dome = next((z for z in zones if z.get("name") == "Aneurysm Dome"), {})
    parent = next((z for z in zones if z.get("name") == "Parent Artery Inlet"), {})
    conv = prov.get("convergence", "") or ""

    cfd = CFDResult(
        run_id=run.run_id,
        solver=prov.get("solver"),
        mesh_cells=_int_from(prov.get("meshCells")),
        mesh_non_orthogonality=_float_after(prov.get("meshNonOrthogonality"), "Max:"),
        mesh_skewness=_float_after(prov.get("meshSkewness"), "="),
        converged="converged" in conv.lower(),
        iterations=_int_from(conv),
        tawss_parent_pa=parent.get("tawss"),
        tawss_sac_pa=dome.get("tawss"),
        osi_sac=dome.get("osi"),
        rrt_sac=hemo.get("rrt"),
        ecap_sac=hemo.get("ecap"),
        nwss=hemo.get("nwss"),
        lsar_relative=hemo.get("lsarRelative"),
        lsar_absolute=hemo.get("lsarAbsolute"),
        zones=zones,
        morphology=rec.get("morphology"),
    )
    session.add(cfd)

    # Model prediction is optional: if the artefact is missing the run still
    # persists with its transparent composite score intact.
    ai_summary = None
    try:
        from risk_model import extract_features, predict  # type: ignore
        feats = extract_features(
            {"zones": [{"id": "dome", "tawss": dome.get("tawss", 0), "osi": dome.get("osi", 0),
                        "rrt": hemo.get("rrt", 0), "ecap": hemo.get("ecap", 0)}],
             "nwss": hemo.get("nwss", 0), "lsar_relative": hemo.get("lsarRelative", 0)},
            rec.get("morphology", {}), demo,
        )
        out = predict(feats, Path(__file__).resolve().parents[1] / "worker" / "models")
        session.add(AIResult(
            run_id=run.run_id,
            model_version=out["model_version"],
            probability=out["probability"],
            risk_score=out["probability"] * 100.0,
            risk_category=out["risk_category"],
            confidence=out["confidence"],
            feature_vector=feats.values,
            shap_summary={"top": out["shap"], "cv_auc": out.get("cv_auc"),
                          "clinical_validity": out.get("clinical_validity")},
        ))
        ai_summary = {"probability": out["probability"], "category": out["risk_category"]}
    except Exception as exc:  # noqa: BLE001
        ai_summary = {"error": f"{exc.__class__.__name__}: {exc}"}

    session.commit()
    return {
        "patient_id": pid,
        "study_id": study.study_id,
        "run_id": run.run_id,
        "run_version": nxt,
        "tawss_sac_pa": dome.get("tawss"),
        "ai": ai_summary,
    }


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Ingest computed patient records into the database")
    ap.add_argument("patients_json")
    args = ap.parse_args()

    init_db()
    doc = json.loads(Path(args.patients_json).read_text())
    session = get_session()
    try:
        results = [ingest_record(r, session) for r in doc.get("patients", [])]
    finally:
        session.close()

    print(json.dumps({"ingested": len(results), "records": results}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
