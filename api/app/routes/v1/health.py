"""Authenticated connection test for the v1 API.

GET /api/v1/health — unlike the root /health (an unauthenticated readiness
probe for orchestration/nginx), this verifies the CALLER'S credential: it
accepts either the master REVA_API_KEY or a per-instance Odoo key and reports
which one matched. Built for "Test connection" buttons (Odoo modules, TUI).

Auth posture mirrors app.dependencies.require_api_key: fail closed (503)
when auth is required but unconfigured; open only in explicit dev mode
(no key configured, auth not required).
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request

from app.dependencies import get_db, get_settings
from app.settings import Settings
from reva.db.engine import Database

router = APIRouter()


@router.get("/health")
def v1_health(
    request: Request,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Report which credential the caller authenticated with, or 401."""
    from app.queries import odoo_instances as q  # local import: avoid a cycle

    if settings.require_api_key and not settings.api_key:
        # Same backstop as require_api_key: never serve unauthenticated when
        # production demands a key that isn't configured.
        raise HTTPException(
            status_code=503, detail="API authentication is required but not configured"
        )

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):]
        resolved = q.resolve_odoo_instance_by_key(db, token)
        if resolved is not None:
            return {"status": "ok", "authenticated_as": "instance", "instance": resolved[1]}
        if settings.api_key and hmac.compare_digest(auth, f"Bearer {settings.api_key}"):
            return {"status": "ok", "authenticated_as": "master", "instance": None}
        raise HTTPException(status_code=401, detail="Invalid API key")

    if not settings.api_key:
        # Explicit dev mode (mirrors require_api_key): open, but say so.
        return {"status": "ok", "authenticated_as": "unauthenticated", "instance": None}
    raise HTTPException(status_code=401, detail="Missing API key")
