"""
Vercel serverless entrypoint for the NeuroFlow API.

Vercel's Python runtime turns any file under `api/` into a serverless function
and looks for a module-level ASGI `app`. This re-exports the FastAPI
application from services/api so there is exactly one implementation rather
than a drifting copy.

Deployed here rather than on a dedicated host because the account already
exists and the read paths the dashboard needs are stateless. What this does NOT
give you is the Celery worker: serverless functions are short-lived and cannot
host a 3-hour CFD solve, so the worker still runs on the machine with OpenFOAM
and reaches the same database outbound.
"""

from __future__ import annotations

import sys
from pathlib import Path

# services/api holds the real implementation.
_SERVICES_API = Path(__file__).resolve().parents[1] / "services" / "api"
sys.path.insert(0, str(_SERVICES_API))

from main import app  # noqa: E402,F401  — re-exported for the Vercel runtime

__all__ = ["app"]
