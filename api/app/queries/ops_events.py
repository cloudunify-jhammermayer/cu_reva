"""Read queries for the ops-event log."""

from __future__ import annotations

from sqlalchemy import func, select

from reva.db.engine import Database
from reva.db.models import OpsEvent


def list_ops_events(
    db: Database,
    component: str | None = None,
    severity: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return (items, total), newest first, optionally filtered."""
    with db.session() as s:
        base = select(OpsEvent)
        count_q = select(func.count()).select_from(OpsEvent)
        if component:
            base = base.where(OpsEvent.component == component)
            count_q = count_q.where(OpsEvent.component == component)
        if severity:
            base = base.where(OpsEvent.severity == severity)
            count_q = count_q.where(OpsEvent.severity == severity)

        total = s.execute(count_q).scalar_one()
        rows = s.execute(
            base.order_by(OpsEvent.created_at.desc(), OpsEvent.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars().all()
        items = [
            {
                "id": r.id,
                "component": r.component,
                "severity": r.severity,
                "event": r.event,
                "detail": r.detail,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    return items, total
