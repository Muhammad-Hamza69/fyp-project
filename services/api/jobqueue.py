"""
Dispatch pipeline work onto the Celery queues.

NOT named `queue.py`: services/api is on sys.path, so a module by that
name shadows the standard library's `queue` for every import in the
process. Celery's amqp transport does `from queue import Queue`, which
then resolves to this file and fails — taking the worker down with an
error that points nowhere near the cause.

WHY THIS IS SEPARATE FROM tasks.py
The API must be able to enqueue without importing the worker. services/worker
pulls in OpenFOAM helpers, VTK, SimpleITK and LightGBM — hundreds of megabytes
that a serverless function cannot carry and does not need. Celery can dispatch
BY NAME over the broker: `send_task("neuroflow.solve_case", ...)` needs the
broker URL and nothing else. So this module holds only that.

FAILS VISIBLY, NOT SILENTLY
If no broker is configured the run is still created and stays QUEUED, and the
response says why. The alternative — accepting the request and quietly doing
nothing — would leave a run that looks submitted and never progresses, which is
indistinguishable from a worker that crashed.
"""

from __future__ import annotations

import os
from typing import Any

REDIS_URL = os.environ.get("REDIS_URL", "")

# Same names tasks.py registers. Dispatch is by string, so the worker's code
# never has to be importable here — but a typo would fail silently on the
# broker, which is why these are constants rather than inline literals.
TASK_INGEST = "neuroflow.ingest_study"
TASK_SEGMENT = "neuroflow.segment_study"
TASK_SOLVE = "neuroflow.solve_case"
TASK_PREDICT = "neuroflow.predict_risk"
TASK_REPORT = "neuroflow.build_report"

# Must match task_routes in tasks.py. If the two disagree the message lands on a
# queue no worker is consuming and the job waits for ever with no error.
QUEUE_OF = {
    TASK_INGEST: "cpu",
    TASK_SEGMENT: "cpu",
    TASK_SOLVE: "cfd",
    TASK_PREDICT: "ai",
    TASK_REPORT: "reports",
}

_app = None


def _client():
    """Lazily build a broker-only Celery client."""
    global _app
    if _app is not None:
        return _app
    if not REDIS_URL:
        return None
    try:
        from celery import Celery
    except ImportError:
        return None

    _app = Celery("neuroflow-api", broker=REDIS_URL)
    _app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        # Hosted Redis bills per command, and Celery's default multi-queue
        # BRPOP polling is aggressive enough to exhaust a free tier in days.
        broker_transport_options={"polling_interval": 5.0},
    )
    return _app


def available() -> bool:
    return _client() is not None


def status() -> dict[str, Any]:
    """Queue state, for /health. Reports the reason when unavailable."""
    if not REDIS_URL:
        return {"enabled": False, "reason": "REDIS_URL is not set",
                "broker": None, "queues": list(set(QUEUE_OF.values()))}
    if _client() is None:
        return {"enabled": False, "reason": "celery is not installed in this runtime",
                "broker": "redis", "queues": list(set(QUEUE_OF.values()))}
    # Deliberately not pinging the broker here. /health is called often and a
    # dead broker would make it hang rather than report — the enqueue path
    # surfaces that instead, where it is actionable.
    return {"enabled": True, "broker": "redis",
            "queues": sorted(set(QUEUE_OF.values()))}


def enqueue(task: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """
    Dispatch one task. Returns what happened rather than raising.

    A queue that is down must not turn a valid API call into a 500: the run
    record is legitimate and should persist so it can be retried. The caller
    reports `queued: false` and the reason.
    """
    app = _client()
    if app is None:
        return {"queued": False,
                "reason": status().get("reason", "queue unavailable")}
    try:
        res = app.send_task(task, args=args, kwargs=kwargs,
                            queue=QUEUE_OF.get(task, "cpu"))
        return {"queued": True, "task_id": res.id,
                "task": task, "queue": QUEUE_OF.get(task, "cpu")}
    except Exception as exc:  # noqa: BLE001
        return {"queued": False,
                "reason": f"{exc.__class__.__name__}: {exc}"}


def enqueue_run(run_id: str, case_dir: str, nproc: int = 6) -> dict[str, Any]:
    """
    Start a run.

    Only the SOLVE is dispatched here. The stages before it (ingest, segment,
    reconstruct) need an uploaded study on disk, and the stages after it
    (predict, report) need the solve's output — chaining them blindly would
    queue work whose inputs do not exist yet. The worker advances the pipeline
    as each stage completes, and job_stages records where it is.
    """
    return enqueue(TASK_SOLVE, run_id, case_dir, nproc)
