"""
SQLAlchemy models — the relational schema from the architecture document.

Design decisions worth stating:

* **Large binaries are NOT stored here.** Meshes, DICOM series and PDFs live in
  object storage; the database holds only keys, checksums and numbers. Postgres
  pages are 8 KB, so multi-megabyte BLOBs bloat the buffer cache and degrade
  every unrelated query.

* **Runs are immutable and versioned.** Re-solving a study creates
  `run_version = n+1` rather than overwriting. Without this you cannot say
  "we refined the mesh and TAWSS moved by X" — the previous evidence is gone.

* **`job_stages` is the durable progress record**, not a websocket side-effect.
  A CFD job outlives many browser sessions; progress must survive a reload.

* **`clerk_org_id` on Patient** carries multi-tenancy even though auth is not
  wired yet — retrofitting a tenant key onto populated tables is far worse.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Postgres gets JSONB (indexable, binary); every other dialect gets plain JSON.
# Declaring JSONB directly makes the models unusable on SQLite, which the test
# suite and offline tooling rely on — create_all fails with
# "can't render element of type JSONB" before a single test runs.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class JobState(str, enum.Enum):
    """One enum shared by the worker, the API and the UI."""
    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    PREPROCESSING = "PREPROCESSING"
    SEGMENTING = "SEGMENTING"
    SEG_VALIDATING = "SEG_VALIDATING"
    RECONSTRUCTING = "RECONSTRUCTING"
    MORPHOLOGY = "MORPHOLOGY"          # before CFD: yields a partial risk score
    MODEL_PREP = "MODEL_PREP"
    MESHING = "MESHING"
    MESH_QC = "MESH_QC"
    BOUNDARY_SETUP = "BOUNDARY_SETUP"
    SOLVING = "SOLVING"
    POSTPROCESSING = "POSTPROCESSING"
    THRESHOLD_EVAL = "THRESHOLD_EVAL"
    FEATURE_EXTRACTION = "FEATURE_EXTRACTION"
    RISK_PREDICTION = "RISK_PREDICTION"
    COMPOSITE_RISK = "COMPOSITE_RISK"
    REPORTING = "REPORTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"


class Patient(Base):
    __tablename__ = "patients"

    patient_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200))
    age: Mapped[int | None] = mapped_column(Integer)
    sex: Mapped[str | None] = mapped_column(String(8))
    hypertension: Mapped[bool] = mapped_column(Boolean, default=False)
    earlier_sah: Mapped[bool] = mapped_column(Boolean, default=False)
    population: Mapped[str] = mapped_column(String(32), default="Other")
    site: Mapped[str | None] = mapped_column(String(32))
    clerk_org_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    studies: Mapped[list["Study"]] = relationship(back_populates="patient", cascade="all, delete-orphan")


class Study(Base):
    __tablename__ = "dicom_studies"

    study_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"), index=True)
    study_date: Mapped[str | None] = mapped_column(String(16))
    modality: Mapped[str | None] = mapped_column(String(16))
    manufacturer: Mapped[str | None] = mapped_column(String(128))
    rows: Mapped[int | None] = mapped_column(Integer)
    columns: Mapped[int | None] = mapped_column(Integer)
    slice_thickness: Mapped[float | None] = mapped_column(Float)
    n_slices: Mapped[int | None] = mapped_column(Integer)
    # Raw imaging stays on the worker (1 GB storage quota); tracked by path+hash.
    local_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    storage_key: Mapped[str | None] = mapped_column(Text)
    quality_score: Mapped[float | None] = mapped_column(Float)
    quality_report: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    patient: Mapped[Patient] = relationship(back_populates="studies")
    runs: Mapped[list["Run"]] = relationship(back_populates="study", cascade="all, delete-orphan")


class Run(Base):
    """One immutable analysis pass over a study."""
    __tablename__ = "runs"
    __table_args__ = (UniqueConstraint("study_id", "run_version", name="uq_run_version"),)

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    study_id: Mapped[str] = mapped_column(ForeignKey("dicom_studies.study_id", ondelete="CASCADE"), index=True)
    run_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[JobState] = mapped_column(Enum(JobState, name="job_state"), default=JobState.QUEUED)
    segmentation_backend: Mapped[str] = mapped_column(String(32), default="traditional")
    mesh_preset: Mapped[str] = mapped_column(String(16), default="coarse")
    rheology: Mapped[str] = mapped_column(String(32), default="newtonian")
    cycles: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    study: Mapped[Study] = relationship(back_populates="runs")
    stages: Mapped[list["JobStage"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class JobStage(Base):
    """Durable per-stage progress. The websocket is a view over this table."""
    __tablename__ = "job_stages"
    __table_args__ = (UniqueConstraint("run_id", "stage", name="uq_run_stage"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"), index=True)
    stage: Mapped[JobState] = mapped_column(Enum(JobState, name="job_state"), nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict | None] = mapped_column(JSONType)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[Run] = relationship(back_populates="stages")


class Artifact(Base):
    """Pointer to a file in object storage (or on the worker). Never the bytes."""
    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"), index=True)
    stage: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[Run] = relationship(back_populates="artifacts")


class SegmentationResult(Base):
    __tablename__ = "segmentation_results"

    seg_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"), index=True)
    backend: Mapped[str] = mapped_column(String(32))
    dice: Mapped[float | None] = mapped_column(Float)
    hausdorff_mm: Mapped[float | None] = mapped_column(Float)
    n_components: Mapped[int | None] = mapped_column(Integer)
    voxel_count: Mapped[int | None] = mapped_column(Integer)
    stl_key: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CFDResult(Base):
    __tablename__ = "cfd_results"

    cfd_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"), index=True)
    solver: Mapped[str | None] = mapped_column(String(64))
    mesh_cells: Mapped[int | None] = mapped_column(Integer)
    mesh_non_orthogonality: Mapped[float | None] = mapped_column(Float)
    mesh_skewness: Mapped[float | None] = mapped_column(Float)
    converged: Mapped[bool] = mapped_column(Boolean, default=False)
    iterations: Mapped[int | None] = mapped_column(Integer)
    # Hemodynamics, all in SI. TAWSS is in PASCALS — converted from OpenFOAM's
    # kinematic m^2/s^2 by multiplying by rho. See pipeline/hemodynamics.py.
    tawss_parent_pa: Mapped[float | None] = mapped_column(Float)
    tawss_sac_pa: Mapped[float | None] = mapped_column(Float)
    osi_sac: Mapped[float | None] = mapped_column(Float)
    rrt_sac: Mapped[float | None] = mapped_column(Float)
    ecap_sac: Mapped[float | None] = mapped_column(Float)
    nwss: Mapped[float | None] = mapped_column(Float)
    lsar_relative: Mapped[float | None] = mapped_column(Float)
    lsar_absolute: Mapped[float | None] = mapped_column(Float)
    zones: Mapped[dict | None] = mapped_column(JSONType)
    morphology: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AIResult(Base):
    __tablename__ = "ai_results"

    pred_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"), index=True)
    model_version: Mapped[str | None] = mapped_column(String(64))
    probability: Mapped[float | None] = mapped_column(Float)
    risk_score: Mapped[float | None] = mapped_column(Float)
    risk_category: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[float | None] = mapped_column(Float)
    feature_vector: Mapped[dict | None] = mapped_column(JSONType)
    shap_summary: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Report(Base):
    __tablename__ = "reports"

    report_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"), index=True)
    storage_key: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    signed_by: Mapped[str | None] = mapped_column(String(128))
    generated_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
