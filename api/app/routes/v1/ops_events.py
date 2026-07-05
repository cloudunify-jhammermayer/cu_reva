"""Ops-event log endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import get_db
from app.pagination import clamp_limit, clamp_offset
from app.queries import ops_events as q
from app.schemas.ops_events import OpsEventEntry, OpsEventPage
from reva.db.engine import Database

router = APIRouter()


@router.get("/ops-events", response_model=OpsEventPage)
def list_ops_events(
    component: str | None = None,
    severity: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Database = Depends(get_db),
) -> dict:
    """Component-degradation events, newest first."""
    limit = clamp_limit(limit, 200)
    offset = clamp_offset(offset)
    items, total = q.list_ops_events(
        db, component=component, severity=severity, limit=limit, offset=offset
    )
    return {"items": [OpsEventEntry.model_validate(i) for i in items], "total": total}
