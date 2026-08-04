"""
NeuroFlow API gateway — FastAPI, versioned at /api/v1.

Versioning policy: within v1 changes are ADDITIVE only. Renaming, removing or
narrowing a field requires /api/v2 served alongside. Every response carries an
X-API-Version header so a client can detect drift.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from auth import Principal, auth_status, current_principal, tenant_filter
from db import get_session, init_db
from models import (
    AIResult, Artifact, CFDResult, JobStage, JobState, Patient, Report,
    Run, SegmentationResult, Study,
)

API_VERSION = "1.0.0"
app = FastAPI(
    title="NeuroFlow CFD API",
    version=API_VERSION,
    description="Cerebral aneurysm hemodynamic analysis — patients, studies, CFD runs and risk scoring.",
    docs_url="/api/v1/docs",
    openapi_url="/api/v1/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_version_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-API-Version"] = API_VERSION
    return response


def db() -> Session:
    s = get_session()
    try:
        yield s
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class PatientIn(BaseModel):
    patient_id: str
    name: str | None = None
    age: int | None = None
    sex: Literal["M", "F", "O"] | None = None
    hypertension: bool = False
    earlier_sah: bool = False
    population: Literal["Other", "Japanese", "Finnish"] = "Other"
    site: Literal["ICA", "MCA", "ACOM_PCOM_POST"] | None = None
    clerk_org_id: str | None = None


class PatientOut(PatientIn):
    created_at: datetime | None = None
    n_studies: int = 0

    model_config = {"from_attributes": True}


class StudyIn(BaseModel):
    study_date: str | None = None
    modality: str | None = None
    manufacturer: str | None = None
    rows: int | None = None
    columns: int | None = None
    slice_thickness: float | None = None
    n_slices: int | None = None
    local_path: str | None = None
    sha256: str | None = None


class RunIn(BaseModel):
    segmentation_backend: Literal["traditional", "monai"] = "traditional"
    mesh_preset: Literal["coarse", "standard", "fine"] = "coarse"
    rheology: Literal["newtonian", "carreau_yasuda"] = "newtonian"
    cycles: int = Field(default=1, ge=1, le=3)


class StageOut(BaseModel):
    stage: str
    state: str
    progress: float
    message: str | None = None
    metrics: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------- #
# Health / meta
# --------------------------------------------------------------------------- #

@app.get("/api/v1/health", tags=["meta"])
def health(s: Session = Depends(db)) -> dict[str, Any]:
    try:
        s.execute(select(func.count()).select_from(Patient))
        database = "up"
    except Exception as exc:
        database = f"down: {exc.__class__.__name__}"
    return {
        "status": "ok" if database == "up" else "degraded",
        "version": API_VERSION,
        "database": database,
        "auth": auth_status(),
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/v1/me", tags=["meta"])
def whoami(p: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Resolve the calling principal and the tenant its queries are scoped to."""
    return {
        "user_id": p.user_id,
        "org_id": p.org_id,
        "email": p.email,
        "dev_mode": p.is_dev,
        "tenant_scope": tenant_filter(p),
    }


@app.get("/api/v1/stages", tags=["meta"])
def list_stages() -> dict[str, list[str]]:
    """The pipeline state machine, so clients need not hardcode it."""
    return {"stages": [s.value for s in JobState]}


# --------------------------------------------------------------------------- #
# Patients
# --------------------------------------------------------------------------- #

@app.get("/api/v1/patients", tags=["patients"])
def list_patients(
    s: Session = Depends(db),
    limit: int = Query(100, le=500),
    principal: Principal = Depends(current_principal),
) -> list[dict]:
    q = select(Patient)
    # Tenant isolation: an authenticated caller sees only its organisation's
    # records. Applied in the query, not filtered afterwards, so a large tenant
    # cannot exhaust the page limit for a small one.
    org = tenant_filter(principal)
    if org:
        q = q.where(Patient.clerk_org_id == org)
    rows = s.execute(q.limit(limit)).scalars().all()
    out = []
    for p in rows:
        d = {c.name: getattr(p, c.name) for c in Patient.__table__.columns}
        d["n_studies"] = len(p.studies)
        out.append(d)
    return out


@app.post("/api/v1/patients", tags=["patients"], status_code=201)
def create_patient(
    body: PatientIn,
    s: Session = Depends(db),
    principal: Principal = Depends(current_principal),
) -> dict:
    if s.get(Patient, body.patient_id):
        raise HTTPException(409, f"patient {body.patient_id} already exists")
    data = body.model_dump()
    # Stamp the caller's tenant rather than trusting the request body — a client
    # must not be able to create records inside another organisation.
    org = tenant_filter(principal)
    if org:
        data["clerk_org_id"] = org
    p = Patient(**data)
    s.add(p); s.commit()
    return {c.name: getattr(p, c.name) for c in Patient.__table__.columns}


@app.get("/api/v1/patients/{patient_id}", tags=["patients"])
def get_patient(patient_id: str, s: Session = Depends(db)) -> dict:
    p = s.get(Patient, patient_id)
    if not p:
        raise HTTPException(404, "patient not found")
    d = {c.name: getattr(p, c.name) for c in Patient.__table__.columns}
    d["studies"] = [
        {c.name: getattr(st, c.name) for c in Study.__table__.columns} for st in p.studies
    ]
    return d


@app.delete("/api/v1/patients/{patient_id}", tags=["patients"], status_code=204)
def delete_patient(patient_id: str, s: Session = Depends(db)) -> None:
    p = s.get(Patient, patient_id)
    if not p:
        raise HTTPException(404, "patient not found")
    s.delete(p); s.commit()


# --------------------------------------------------------------------------- #
# Studies
# --------------------------------------------------------------------------- #

@app.post("/api/v1/patients/{patient_id}/studies", tags=["studies"], status_code=201)
def create_study(patient_id: str, body: StudyIn, s: Session = Depends(db)) -> dict:
    if not s.get(Patient, patient_id):
        raise HTTPException(404, "patient not found")
    st = Study(patient_id=patient_id, **body.model_dump())
    s.add(st); s.commit()
    return {c.name: getattr(st, c.name) for c in Study.__table__.columns}


@app.get("/api/v1/studies/{study_id}", tags=["studies"])
def get_study(study_id: str, s: Session = Depends(db)) -> dict:
    st = s.get(Study, study_id)
    if not st:
        raise HTTPException(404, "study not found")
    d = {c.name: getattr(st, c.name) for c in Study.__table__.columns}
    d["runs"] = [
        {"run_id": r.run_id, "run_version": r.run_version, "state": r.state.value}
        for r in st.runs
    ]
    return d


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #

@app.post("/api/v1/studies/{study_id}/runs", tags=["runs"], status_code=201)
def create_run(study_id: str, body: RunIn, s: Session = Depends(db)) -> dict:
    st = s.get(Study, study_id)
    if not st:
        raise HTTPException(404, "study not found")
    # Immutable versioning: never overwrite a previous run.
    nxt = (s.execute(
        select(func.coalesce(func.max(Run.run_version), 0)).where(Run.study_id == study_id)
    ).scalar() or 0) + 1
    r = Run(study_id=study_id, run_version=nxt, **body.model_dump())
    s.add(r); s.flush()
    for stage in (JobState.VALIDATING, JobState.PREPROCESSING, JobState.SEGMENTING,
                  JobState.RECONSTRUCTING, JobState.MORPHOLOGY, JobState.MESHING,
                  JobState.SOLVING, JobState.POSTPROCESSING, JobState.RISK_PREDICTION,
                  JobState.REPORTING):
        s.add(JobStage(run_id=r.run_id, stage=stage, state="pending"))
    s.commit()
    return {"run_id": r.run_id, "run_version": r.run_version, "state": r.state.value}


@app.get("/api/v1/runs/{run_id}", tags=["runs"])
def get_run(run_id: str, s: Session = Depends(db)) -> dict:
    r = s.get(Run, run_id)
    if not r:
        raise HTTPException(404, "run not found")
    return {
        "run_id": r.run_id, "study_id": r.study_id, "run_version": r.run_version,
        "state": r.state.value, "error": r.error,
        "segmentation_backend": r.segmentation_backend,
        "mesh_preset": r.mesh_preset, "rheology": r.rheology, "cycles": r.cycles,
        "stages": [
            {"stage": g.stage.value, "state": g.state, "progress": g.progress,
             "message": g.message, "metrics": g.metrics}
            for g in sorted(r.stages, key=lambda x: list(JobState).index(x.stage))
        ],
        "artifacts": [
            {"kind": a.kind, "storage_key": a.storage_key, "local_path": a.local_path,
             "bytes": a.bytes, "sha256": a.sha256}
            for a in r.artifacts
        ],
    }


@app.get("/api/v1/runs/{run_id}/stages", tags=["runs"])
def get_stages(run_id: str, s: Session = Depends(db)) -> list[dict]:
    """
    Durable progress. Deliberately readable without a websocket: a CFD job can
    run for hours and outlive many browser sessions, so progress must survive a
    reload rather than living only in a stream.
    """
    r = s.get(Run, run_id)
    if not r:
        raise HTTPException(404, "run not found")
    return [
        {"stage": g.stage.value, "state": g.state, "progress": g.progress,
         "message": g.message, "metrics": g.metrics,
         "started_at": g.started_at, "ended_at": g.ended_at}
        for g in sorted(r.stages, key=lambda x: list(JobState).index(x.stage))
    ]


@app.post("/api/v1/runs/{run_id}/cancel", tags=["runs"])
def cancel_run(run_id: str, s: Session = Depends(db)) -> dict:
    r = s.get(Run, run_id)
    if not r:
        raise HTTPException(404, "run not found")
    if r.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
        return {"run_id": run_id, "state": r.state.value, "note": "already terminal"}
    r.state = JobState.CANCELLING
    s.commit()
    return {"run_id": run_id, "state": r.state.value}


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

@app.get("/api/v1/runs/{run_id}/cfd", tags=["results"])
def get_cfd(run_id: str, s: Session = Depends(db)) -> dict:
    row = s.execute(select(CFDResult).where(CFDResult.run_id == run_id)).scalars().first()
    if not row:
        raise HTTPException(404, "no CFD result for this run")
    return {c.name: getattr(row, c.name) for c in CFDResult.__table__.columns}


@app.get("/api/v1/runs/{run_id}/segmentation", tags=["results"])
def get_segmentation(run_id: str, s: Session = Depends(db)) -> dict:
    row = s.execute(select(SegmentationResult).where(SegmentationResult.run_id == run_id)).scalars().first()
    if not row:
        raise HTTPException(404, "no segmentation result for this run")
    return {c.name: getattr(row, c.name) for c in SegmentationResult.__table__.columns}


@app.get("/api/v1/runs/{run_id}/risk", tags=["results"])
def get_risk(run_id: str, s: Session = Depends(db)) -> dict:
    ai = s.execute(select(AIResult).where(AIResult.run_id == run_id)).scalars().first()
    cfd = s.execute(select(CFDResult).where(CFDResult.run_id == run_id)).scalars().first()
    if not cfd:
        raise HTTPException(404, "no results for this run")
    out: dict[str, Any] = {
        "hemodynamics": {
            "tawss_parent_pa": cfd.tawss_parent_pa,
            "tawss_sac_pa": cfd.tawss_sac_pa,
            "osi_sac": cfd.osi_sac,
            "rrt_sac": cfd.rrt_sac,
            "ecap_sac": cfd.ecap_sac,
            "nwss": cfd.nwss,
            "lsar_relative": cfd.lsar_relative,
            "lsar_absolute": cfd.lsar_absolute,
        },
        "morphology": cfd.morphology,
        "zones": cfd.zones,
    }
    if ai:
        out["ml"] = {
            "model_version": ai.model_version,
            "probability": ai.probability,
            "risk_category": ai.risk_category,
            "confidence": ai.confidence,
            "shap": ai.shap_summary,
        }
    return out


@app.get("/api/v1/reports/{report_id}", tags=["reports"])
def get_report(report_id: str, s: Session = Depends(db)) -> dict:
    r = s.get(Report, report_id)
    if not r:
        raise HTTPException(404, "report not found")
    return {c.name: getattr(r, c.name) for c in Report.__table__.columns}


# --------------------------------------------------------------------------- #
# Dashboard feed — the shape the existing vanilla frontend consumes
# --------------------------------------------------------------------------- #

@app.get("/api/v1/dashboard/patients", tags=["dashboard"])
def dashboard_feed(s: Session = Depends(db)) -> dict:
    """
    Serve computed cases in the exact shape app.js expects, so the existing
    dashboard can read live database records instead of a static JSON file
    without any change to its rendering code.
    """
    patients = []
    rows = s.execute(
        select(CFDResult, Run, Study, Patient)
        .join(Run, CFDResult.run_id == Run.run_id)
        .join(Study, Run.study_id == Study.study_id)
        .join(Patient, Study.patient_id == Patient.patient_id)
        .order_by(CFDResult.created_at.desc())
    ).all()
    for cfd, run, study, patient in rows:
        if not cfd.zones:
            continue
        patients.append({
            "id": patient.patient_id,
            "morphology": cfd.morphology or {},
            "demographics": {
                "age": patient.age, "hypertension": patient.hypertension,
                "earlierSAH": patient.earlier_sah, "population": patient.population,
                "site": patient.site,
            },
            "zones": cfd.zones,
            "provenance": {
                "source": "computed",
                "solver": cfd.solver,
                "meshCells": cfd.mesh_cells,
                "convergence": f"converged in {cfd.iterations} iterations" if cfd.converged else "not converged",
                "runVersion": run.run_version,
            },
        })
    return {"generatedAt": datetime.now(timezone.utc).isoformat(), "patients": patients}


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"error": exc.__class__.__name__, "detail": str(exc)})


if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run(app, host="0.0.0.0", port=8000)
