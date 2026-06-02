"""FastAPI dependency providers — pull singletons from app.state."""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Request

from app.settings import Settings
from reva.db.engine import Database

if TYPE_CHECKING:
    from rq import Queue


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_queue(request: Request) -> "Queue":
    return request.app.state.rq_queue


def get_redis(request: Request):
    """The Redis connection backing the RQ queue (used by the health check)."""
    return request.app.state.rq_queue.connection


def actor_from_request(request: Request) -> str:
    """Best-effort caller identity for the admin audit log.

    Assumes a single trusted proxy (nginx). nginx sets X-Real-IP to the real
    socket peer ($remote_addr) — the client cannot forge it — so prefer that.
    The LEFT-most X-Forwarded-For entry is client-controlled (nginx only
    *appends* the real hop), so we never trust it for the audit actor (SECU-10);
    if X-Real-IP is somehow absent we use the RIGHT-most XFF hop (added by our
    proxy), then the socket peer.
    """
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def get_github_client(request: Request):
    return request.app.state.github


def require_api_key(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Validate the Bearer token, failing closed when auth is required.

    - No key configured + auth required (REVA_REQUIRE_API_KEY) → 503, never
      serve unauthenticated. (Startup also refuses to boot in this state; this
      is the request-layer backstop so the dependency itself is the gate.)
    - No key configured + auth not required → open (explicit dev mode).
    - Key configured → the Bearer token must match.
    """
    if not settings.api_key:
        if settings.require_api_key:
            raise HTTPException(status_code=503, detail="API authentication is required but not configured")
        return
    auth = request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth, f"Bearer {settings.api_key}"):
        raise HTTPException(status_code=401, detail="Invalid API key")
