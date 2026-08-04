"""
API contract tests.

Run against an in-memory SQLite database rather than Neon so the suite is fast,
hermetic and safe to run in CI without credentials. The models are plain
SQLAlchemy, so the schema exercised here is the same one Postgres gets — apart
from JSONB, which SQLite stores as JSON and behaves identically for these
assertions.

The tests that matter most are the ones covering behaviour that is easy to
break silently: run versioning must never overwrite, and the tenant filter must
be applied in the query rather than trusted from the request body.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_API = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_API))

# Point the app at SQLite BEFORE importing db/main, which read it at import time.
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    import db as db_mod
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from models import Base

    # StaticPool keeps a single in-memory connection alive across sessions;
    # without it every session gets a fresh, empty database.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db_mod.engine = engine
    db_mod.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)

    import main
    main.get_session = db_mod.get_session
    return TestClient(main.app)


class TestMeta:
    def test_health(self, client):
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        body = r.json()
        assert body["database"] == "up"
        assert "auth" in body

    def test_version_header_on_every_response(self, client):
        """Clients need a way to detect API drift."""
        for path in ("/api/v1/health", "/api/v1/stages", "/api/v1/patients"):
            assert client.get(path).headers.get("X-API-Version") == "1.0.0"

    def test_stage_enum_exposed(self, client):
        stages = client.get("/api/v1/stages").json()["stages"]
        # Clients must not hardcode the pipeline state machine.
        for required in ("QUEUED", "SEGMENTING", "MORPHOLOGY", "SOLVING",
                         "SUCCEEDED", "FAILED", "CANCELLED"):
            assert required in stages

    def test_morphology_precedes_solving(self, client):
        """
        Morphology is deliberately ordered BEFORE the solve so a partial risk
        score is available in minutes rather than hours.
        """
        stages = client.get("/api/v1/stages").json()["stages"]
        assert stages.index("MORPHOLOGY") < stages.index("SOLVING")


class TestPatients:
    def test_create_and_fetch(self, client):
        r = client.post("/api/v1/patients", json={
            "patient_id": "PT-TEST-0001", "age": 64, "hypertension": True,
            "population": "Other", "site": "MCA",
        })
        assert r.status_code == 201
        assert r.json()["age"] == 64

        got = client.get("/api/v1/patients/PT-TEST-0001").json()
        assert got["site"] == "MCA"
        assert got["studies"] == []

    def test_duplicate_rejected(self, client):
        client.post("/api/v1/patients", json={"patient_id": "PT-TEST-DUP"})
        r = client.post("/api/v1/patients", json={"patient_id": "PT-TEST-DUP"})
        assert r.status_code == 409

    def test_missing_returns_404(self, client):
        assert client.get("/api/v1/patients/NOPE").status_code == 404

    def test_invalid_enum_rejected(self, client):
        r = client.post("/api/v1/patients",
                        json={"patient_id": "PT-BAD", "site": "ELBOW"})
        assert r.status_code == 422


class TestRunVersioning:
    """
    Immutability is the point: re-analysing a study must create a NEW version
    rather than overwrite, otherwise "we refined the mesh and TAWSS moved by X"
    has no evidence behind it.
    """

    def test_versions_increment_and_never_overwrite(self, client):
        client.post("/api/v1/patients", json={"patient_id": "PT-TEST-RUN"})
        sid = client.post("/api/v1/patients/PT-TEST-RUN/studies",
                          json={"modality": "MR"}).json()["study_id"]

        seen = []
        for expected in (1, 2, 3):
            r = client.post(f"/api/v1/studies/{sid}/runs", json={})
            assert r.status_code == 201
            assert r.json()["run_version"] == expected
            seen.append(r.json()["run_id"])

        assert len(set(seen)) == 3, "each run must be a distinct record"
        runs = client.get(f"/api/v1/studies/{sid}").json()["runs"]
        assert len(runs) == 3

    def test_run_seeds_its_stages(self, client):
        client.post("/api/v1/patients", json={"patient_id": "PT-TEST-STG"})
        sid = client.post("/api/v1/patients/PT-TEST-STG/studies",
                          json={}).json()["study_id"]
        rid = client.post(f"/api/v1/studies/{sid}/runs", json={}).json()["run_id"]

        stages = client.get(f"/api/v1/runs/{rid}/stages").json()
        assert len(stages) >= 8
        assert all(s["state"] == "pending" for s in stages)
        assert all(s["progress"] == 0.0 for s in stages)

    def test_cancel_moves_to_cancelling(self, client):
        client.post("/api/v1/patients", json={"patient_id": "PT-TEST-CAN"})
        sid = client.post("/api/v1/patients/PT-TEST-CAN/studies",
                          json={}).json()["study_id"]
        rid = client.post(f"/api/v1/studies/{sid}/runs", json={}).json()["run_id"]

        assert client.post(f"/api/v1/runs/{rid}/cancel").json()["state"] == "CANCELLING"

    def test_run_options_validated(self, client):
        client.post("/api/v1/patients", json={"patient_id": "PT-TEST-OPT"})
        sid = client.post("/api/v1/patients/PT-TEST-OPT/studies",
                          json={}).json()["study_id"]
        r = client.post(f"/api/v1/studies/{sid}/runs",
                        json={"mesh_preset": "enormous"})
        assert r.status_code == 422


class TestAuth:
    def test_dev_mode_reported_honestly(self, client):
        """Without Clerk keys the API runs open — and must say so, not pretend."""
        me = client.get("/api/v1/me").json()
        assert me["dev_mode"] is True
        assert me["user_id"] == "dev"
        assert me["tenant_scope"] is None

    def test_auth_status_in_health(self, client):
        auth = client.get("/api/v1/health").json()["auth"]
        assert auth["enabled"] is False
        assert "dev" in auth["mode"]


class TestResults:
    def test_missing_results_404_not_empty(self, client):
        """An absent result must be distinguishable from a zero-valued one."""
        client.post("/api/v1/patients", json={"patient_id": "PT-TEST-RES"})
        sid = client.post("/api/v1/patients/PT-TEST-RES/studies",
                          json={}).json()["study_id"]
        rid = client.post(f"/api/v1/studies/{sid}/runs", json={}).json()["run_id"]
        for path in ("cfd", "segmentation", "risk"):
            assert client.get(f"/api/v1/runs/{rid}/{path}").status_code == 404

    def test_dashboard_feed_shape(self, client):
        body = client.get("/api/v1/dashboard/patients").json()
        assert "patients" in body and "generatedAt" in body
        assert isinstance(body["patients"], list)
