"""
FastAPI dependency gating routes on a valid internal service token
(SEC-SERVICE-AUTH). See ``opencomplai_core.service_auth`` for the token
format and the shared-secret distribution model.

Fails closed: if ``INTERNAL_SERVICE_TOKEN_SECRET`` is not configured, every
gated route refuses requests rather than silently running open — the same
"refuse to start/serve rather than run unauthenticated" posture gateway-api's
``auth.ts`` and dashboard-saas's ``dashboard_auth`` already use, modulo the
``OPENCOMPLAI_AUTH_DISABLED`` local-dev bypass those use for interactive
development. There is no equivalent bypass here: these are service-to-service
routes with no interactive caller, so local dev runs docker-compose with the
secret set like every other environment.
"""

from __future__ import annotations

from fastapi import Header, HTTPException
from opencomplai_core.service_auth import (
    ServiceTokenError,
    load_shared_secret,
    verify_service_token,
)


def require_service_principal(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency: returns the calling service's issuer name, or raises 401/503."""
    secret = load_shared_secret()
    if secret is None:
        raise HTTPException(
            status_code=503,
            detail="INTERNAL_SERVICE_TOKEN_SECRET is not configured",
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing service token")

    try:
        claims = verify_service_token(authorization[7:], secret)
    except ServiceTokenError as exc:
        raise HTTPException(status_code=401, detail="invalid service token") from exc

    return claims.issuer
