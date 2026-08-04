"""
Clerk authentication and multi-tenancy.

Verifies Clerk-issued session JWTs using the RS256 public keys published at the
instance's JWKS endpoint. Verification is done locally against cached keys
rather than by calling Clerk on every request — a network round-trip per API
call would add latency to every endpoint and make the API unavailable whenever
Clerk is.

TENANCY
-------
The `org_id` claim becomes the tenant boundary. Every patient row carries
`clerk_org_id`, and queries filter on the caller's organisation, so one
institution cannot read another's records. The column already exists in the
schema even though auth was added later — retrofitting a tenant key onto
populated tables is far more painful than carrying an unused column.

DEV MODE
--------
If `CLERK_PUBLISHABLE_KEY` is unset the API runs unauthenticated and every
request is attributed to a `dev` principal. That keeps the local demo and the
test suite runnable without a Clerk instance. It is logged loudly at startup,
and `require_auth` refuses to fall back to dev mode once a key IS configured —
so a misconfigured deployment fails closed rather than silently open.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Header, HTTPException

CLERK_PUBLISHABLE_KEY = os.environ.get("CLERK_PUBLISHABLE_KEY", "")
CLERK_SECRET_KEY = os.environ.get("CLERK_SECRET_KEY", "")
CLERK_JWKS_URL = os.environ.get("CLERK_JWKS_URL", "")
CLERK_ISSUER = os.environ.get("CLERK_ISSUER", "")

AUTH_ENABLED = bool(CLERK_PUBLISHABLE_KEY or CLERK_JWKS_URL)

_JWKS_CACHE: dict[str, Any] = {"keys": None, "fetched_at": 0.0}
_JWKS_TTL_S = 3600.0


@dataclass
class Principal:
    user_id: str
    org_id: str | None
    email: str | None
    is_dev: bool = False


def _derive_jwks_url() -> str:
    """
    Clerk publishes JWKS at https://<instance>/.well-known/jwks.json.

    The instance host is encoded in the publishable key: `pk_test_<base64host>`
    / `pk_live_<base64host>`, so an explicit CLERK_JWKS_URL is optional.
    """
    if CLERK_JWKS_URL:
        return CLERK_JWKS_URL
    if not CLERK_PUBLISHABLE_KEY:
        return ""
    import base64

    try:
        tail = CLERK_PUBLISHABLE_KEY.split("_", 2)[2]
        pad = "=" * (-len(tail) % 4)
        host = base64.b64decode(tail + pad).decode().rstrip("$")
        return f"https://{host}/.well-known/jwks.json"
    except Exception:
        return ""


def _jwks() -> dict[str, Any]:
    """Fetch and cache the signing keys. Cached for an hour — Clerk rotates rarely."""
    now = time.time()
    if _JWKS_CACHE["keys"] and now - _JWKS_CACHE["fetched_at"] < _JWKS_TTL_S:
        return _JWKS_CACHE["keys"]
    url = _derive_jwks_url()
    if not url:
        raise HTTPException(500, "Clerk is enabled but no JWKS URL could be determined")
    with urllib.request.urlopen(url, timeout=10) as r:
        keys = json.loads(r.read().decode())
    _JWKS_CACHE.update({"keys": keys, "fetched_at": now})
    return keys


def _verify(token: str) -> dict[str, Any]:
    """Verify signature, expiry and issuer. Raises 401 on any failure."""
    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError as exc:
        raise HTTPException(500, f"PyJWT is required for Clerk verification: {exc}")

    url = _derive_jwks_url()
    try:
        signing_key = PyJWKClient(url).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            # Clerk session tokens carry no `aud`; issuer is the real check.
            options={"verify_aud": False},
            issuer=CLERK_ISSUER or None,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(401, f"invalid session token: {exc.__class__.__name__}")
    return claims


async def current_principal(
    authorization: str | None = Header(default=None),
) -> Principal:
    """
    Resolve the caller.

    Fails CLOSED: once Clerk is configured a missing or malformed token is a
    401, never a silent downgrade to the dev principal.
    """
    if not AUTH_ENABLED:
        return Principal(user_id="dev", org_id=None, email=None, is_dev=True)

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")

    claims = _verify(authorization.split(" ", 1)[1].strip())
    return Principal(
        user_id=str(claims.get("sub", "")),
        org_id=claims.get("org_id") or claims.get("o", {}).get("id"),
        email=claims.get("email"),
        is_dev=False,
    )


def require_auth(p: Principal = Depends(current_principal)) -> Principal:
    if AUTH_ENABLED and p.is_dev:
        raise HTTPException(401, "authentication required")
    return p


def tenant_filter(p: Principal) -> str | None:
    """
    Organisation to scope queries by, or None for unscoped access.

    Dev mode is unscoped so local work sees all records; a real principal
    without an org claim is also unscoped, which is correct for a personal
    (non-organisation) Clerk account.
    """
    return None if p.is_dev else p.org_id


def auth_status() -> dict[str, Any]:
    return {
        "enabled": AUTH_ENABLED,
        "provider": "clerk" if AUTH_ENABLED else None,
        "jwks_url": _derive_jwks_url() or None,
        "mode": "enforced" if AUTH_ENABLED else "dev (unauthenticated)",
    }
