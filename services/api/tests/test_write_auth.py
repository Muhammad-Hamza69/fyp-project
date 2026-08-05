"""
Tests for the write gate.

WHAT WENT WRONG
The deployed API had `AUTH_ENABLED == False`, because that flag is derived from
Clerk environment variables that were never set on the hosting platform. With
auth off, `current_principal` resolves every caller to the dev principal — which
is correct and deliberate for local work. The mutating routes, however, did not
depend on the principal at all: `delete_patient`, `create_study`, `create_run`
and `cancel_run` took only a database session. The result was a public endpoint
where an unauthenticated `curl -X DELETE` removed a patient from the live
database.

Reads are meant to be open — this is a public demonstration over synthetic data.
Writes are not, and the distinction is what these tests pin.

The important case is the LAST one: writes must be refused when no auth provider
is configured. Failing closed there is what turns a silent vulnerability into a
visible, diagnosable 503.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

_API = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_API))

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"


def _client(*, allow_dev_writes: bool):
    """
    Build an app whose auth module has been re-imported under the given
    environment. auth.py reads its flags at import time, so the module must be
    reloaded for a change to take effect.
    """
    os.environ["NEUROFLOW_ALLOW_DEV_WRITES"] = "1" if allow_dev_writes else "0"
    for mod in ("auth", "main"):
        if mod in sys.modules:
            del sys.modules[mod]

    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import db as db_mod
    importlib.reload(db_mod)
    from models import Base

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db_mod.get_session = sessionmaker(bind=engine, expire_on_commit=False)

    import main
    return TestClient(main.app)


PATIENT = {"patient_id": "PT-GATE-01", "age": 60, "site": "MCA"}


# --- closed by default ------------------------------------------------------

def test_create_is_refused_without_an_auth_provider():
    c = _client(allow_dev_writes=False)
    r = c.post("/api/v1/patients", json=PATIENT)
    assert r.status_code == 503
    assert "authentication provider" in r.json()["detail"]


def test_delete_is_refused_without_an_auth_provider():
    """
    The route that mattered most: it took no principal at all, so anyone could
    remove a patient from the deployed database.
    """
    c = _client(allow_dev_writes=False)
    assert c.delete("/api/v1/patients/PT-GATE-01").status_code == 503


def test_create_study_and_run_are_refused():
    c = _client(allow_dev_writes=False)
    assert c.post("/api/v1/patients/PT-GATE-01/studies", json={}).status_code == 503
    assert c.post("/api/v1/studies/ST-1/runs", json={}).status_code == 503


def test_cancel_is_refused():
    """Cancelling someone else's run is a mutation like any other."""
    c = _client(allow_dev_writes=False)
    assert c.post("/api/v1/runs/RUN-1/cancel").status_code == 503


def test_refusal_precedes_the_404():
    """
    The gate must run BEFORE the handler looks anything up. If it did not, an
    unauthenticated caller could probe which patient IDs exist by telling 404
    from 503 — a small leak, but a free one to close.
    """
    c = _client(allow_dev_writes=False)
    assert c.delete("/api/v1/patients/DEFINITELY-NOT-A-REAL-ID").status_code == 503


# --- reads stay open --------------------------------------------------------

def test_reads_are_unaffected():
    """
    The dashboard is a public demonstration and is meant to be readable. Closing
    reads too would break the deployed page for no security benefit — the data
    is synthetic.
    """
    c = _client(allow_dev_writes=False)
    for path in ("/api/v1/health", "/api/v1/stages", "/api/v1/patients"):
        assert c.get(path).status_code == 200, path


def test_health_reports_auth_as_disabled():
    """The state must be visible, not merely true."""
    c = _client(allow_dev_writes=False)
    auth = c.get("/api/v1/health").json()["auth"]
    assert auth["enabled"] is False
    assert "unauthenticated" in auth["mode"]


# --- the explicit local escape hatch ---------------------------------------

def test_writes_work_when_dev_writes_are_explicitly_enabled():
    c = _client(allow_dev_writes=True)
    assert c.post("/api/v1/patients", json=PATIENT).status_code == 201
    assert c.delete("/api/v1/patients/PT-GATE-01").status_code == 204


def test_the_escape_hatch_is_opt_in_not_default():
    """
    A default-on escape hatch would reproduce the original bug exactly, so the
    flag must require the literal "1".
    """
    import auth
    for value in ("", "0", "true", "yes", "TRUE"):
        os.environ["NEUROFLOW_ALLOW_DEV_WRITES"] = value
        importlib.reload(auth)
        assert auth.ALLOW_DEV_WRITES is False, f"{value!r} should not enable writes"

    os.environ["NEUROFLOW_ALLOW_DEV_WRITES"] = "1"
    importlib.reload(auth)
    assert auth.ALLOW_DEV_WRITES is True


@pytest.fixture(autouse=True)
def _restore_env():
    yield
    os.environ["NEUROFLOW_ALLOW_DEV_WRITES"] = "1"
