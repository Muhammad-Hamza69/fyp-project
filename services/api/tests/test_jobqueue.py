"""
Tests for job dispatch.

The defect this covers is not a crash — it is silence. POST /runs created the
run row and its ten stage rows and then stopped: nothing ever dispatched the
work. The state machine, the stage table and all five worker tasks existed and
were correct; the single missing link was the call that hands the job to the
broker. Every run sat at QUEUED for ever, and nothing in the API said so.

So these assert the CONNECTIONS: that dispatch is attempted, that it lands on
the queue a worker is actually consuming, and that a broker which is down is
reported rather than swallowed.

They do not require a running Redis — the routing table and the failure
behaviour are what matter here. The live round trip (API -> broker -> worker ->
result) is exercised separately against a real broker.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

_API = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_API))


def _fresh(redis_url: str):
    """Re-import with a given REDIS_URL; the module reads it at import time."""
    os.environ["REDIS_URL"] = redis_url
    for m in ("jobqueue",):
        if m in sys.modules:
            del sys.modules[m]
    import jobqueue
    return jobqueue


# --- the module must not shadow the standard library ------------------------

def test_module_is_not_named_queue():
    """
    services/api is on sys.path, so a module called `queue.py` there shadows the
    standard library's `queue` for the whole process. Celery's amqp transport
    does `from queue import Queue`, which then resolves to the wrong file and
    the worker dies with an error pointing nowhere near the cause. This happened.
    """
    assert not (_API / "queue.py").exists(), (
        "services/api/queue.py shadows the stdlib `queue` module and breaks Celery"
    )
    assert (_API / "jobqueue.py").exists()


def test_stdlib_queue_still_importable():
    import queue as stdlib_queue
    assert hasattr(stdlib_queue, "Queue")


# --- routing ----------------------------------------------------------------

def test_every_task_has_a_queue():
    """
    A task dispatched to a queue no worker consumes waits for ever, with no
    error anywhere. The mapping must be total.
    """
    jq = _fresh("")
    for task in (jq.TASK_INGEST, jq.TASK_SEGMENT, jq.TASK_SOLVE,
                 jq.TASK_PREDICT, jq.TASK_REPORT):
        assert task in jq.QUEUE_OF, f"{task} has no queue"


def test_routing_matches_the_worker():
    """
    The API's routing table and the worker's `task_routes` are declared in
    separate files. If they drift, messages land on a queue nobody is reading
    and the job hangs silently — so they are asserted against each other.
    """
    jq = _fresh("")
    worker_src = (_API.parent / "worker" / "tasks.py").read_text(encoding="utf-8")
    for task, q in jq.QUEUE_OF.items():
        # e.g.  "neuroflow.solve_case": {"queue": "cfd"}
        needle = f'"{task}": {{"queue": "{q}"}}'
        assert needle in worker_src, (
            f"{task} routes to '{q}' in the API but not in tasks.py"
        )


def test_solve_goes_to_the_cfd_queue():
    """
    The CFD queue is run at concurrency 1 so a six-core solve does not contend
    with itself. Routing a solve anywhere else would silently break that.
    """
    jq = _fresh("")
    assert jq.QUEUE_OF[jq.TASK_SOLVE] == "cfd"


# --- behaviour without a broker ---------------------------------------------

def test_reports_unavailable_when_no_broker_configured():
    jq = _fresh("")
    st = jq.status()
    assert st["enabled"] is False
    assert "REDIS_URL" in st["reason"]
    assert jq.available() is False


def test_enqueue_reports_failure_instead_of_raising():
    """
    A queue that is down must not turn a valid API call into a 500. The run
    record is legitimate and should persist so it can be retried; the response
    carries `queued: false` and the reason.
    """
    jq = _fresh("")
    out = jq.enqueue(jq.TASK_SOLVE, "RUN-1", "/tmp/case", 6)
    assert out["queued"] is False
    assert out["reason"]


def test_enqueue_run_dispatches_only_the_solve():
    """
    Chaining the whole pipeline up front would queue stages whose inputs do not
    exist yet — predict and report need the solve's output. The worker advances
    the pipeline as each stage lands.
    """
    jq = _fresh("")
    src = (_API / "jobqueue.py").read_text(encoding="utf-8")
    assert "TASK_SOLVE" in src.split("def enqueue_run")[1]
    for later in ("TASK_PREDICT", "TASK_REPORT"):
        assert later not in src.split("def enqueue_run")[1], (
            f"enqueue_run dispatches {later} before its inputs exist"
        )


# --- the API surfaces it ----------------------------------------------------

def test_health_reports_queue_state():
    """The state must be visible, not merely true — the same rule as auth."""
    os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
    os.environ["NEUROFLOW_ALLOW_DEV_WRITES"] = "1"
    os.environ["REDIS_URL"] = ""
    for m in ("jobqueue", "main"):
        if m in sys.modules:
            del sys.modules[m]

    from fastapi.testclient import TestClient
    import main
    body = TestClient(main.app).get("/api/v1/health").json()
    assert "queue" in body, "health does not report queue state"
    assert body["queue"]["enabled"] is False
    assert "queues" in body["queue"]


def test_create_run_reports_dispatch_outcome():
    """
    The regression test for the original defect: the response must say whether
    the job was actually handed to a worker. Returning only the run id was what
    made a run that never started look identical to one that had.
    """
    src = (_API / "main.py").read_text(encoding="utf-8")
    body = src.split("def create_run")[1].split("@app.")[0]
    assert "enqueue_run" in body, "create_run does not dispatch the job"
    assert "dispatch" in body, "create_run does not report the dispatch outcome"
