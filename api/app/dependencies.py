"""FastAPI dependency providers — pull singletons from app.state."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


@dataclass(frozen=True)
class ResolvedOdooInstance:
    id: int
    name: str
    # Per-instance quotas; None = unlimited.
    daily_budget_usd: float | None = None
    rate_limit_per_minute: int | None = None


def require_odoo_instance(
    request: Request, db: Database = Depends(get_db)
) -> ResolvedOdooInstance:
    """Resolve the calling Odoo instance from its Bearer key, or 401.

    The instance key IS the identity. The master key does not resolve here (it
    is not an instance), so it is correctly rejected on the create routes.
    """
    from app.queries import odoo_instances as q  # local import: avoid a cycle

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Odoo instance key")
    token = auth[len("Bearer "):]
    resolved = q.resolve_odoo_instance_by_key(db, token)
    if resolved is None:
        raise HTTPException(status_code=401, detail="Invalid Odoo instance key")
    budget, rpm = q.instance_limits(db, resolved[0])
    from app.ratelimit import enforce_instance_rate_limit  # local: avoid cycle

    enforce_instance_rate_limit(resolved[0], rpm)
    return ResolvedOdooInstance(
        id=resolved[0],
        name=resolved[1],
        daily_budget_usd=budget,
        rate_limit_per_minute=rpm,
    )


def assert_instance_within_budget(db: Database, instance: ResolvedOdooInstance) -> None:
    """429 when the instance's rolling-24h spend has reached its cap."""
    if instance.daily_budget_usd is None:
        return
    from reva.db import writers

    spent = writers.sum_instance_cost_since(
        db, instance.id, datetime.now(timezone.utc) - timedelta(days=1)
    )
    if spent >= instance.daily_budget_usd:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Odoo instance daily budget reached "
                f"(~${spent:.2f} of ${instance.daily_budget_usd:.2f} in 24h); "
                f"try again after spend rolls off or raise the cap."
            ),
        )


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
