"""Read queries for odoo_instances: key resolution, list, and cost rollups."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from reva.db.engine import Database
from reva.db.models import OdooInstance, TicketAnalysis, TicketIssueRun


def resolve_odoo_instance_by_key(db: Database, token: str) -> tuple[int, str] | None:
    """Return (id, name) for the ACTIVE instance whose inbound key is `token`."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    with db.session() as s:
        row = s.execute(
            select(OdooInstance.id, OdooInstance.name).where(
                OdooInstance.key_hash == digest,
                OdooInstance.active.is_(True),
            )
        ).first()
        return (row[0], row[1]) if row is not None else None


def instance_limits(db: Database, instance_id: int) -> tuple[float | None, int | None]:
    """Return (daily_budget_usd, rate_limit_per_minute) for one instance."""
    with db.session() as s:
        row = s.execute(
            select(OdooInstance.daily_budget_usd, OdooInstance.rate_limit_per_minute)
            .where(OdooInstance.id == instance_id)
        ).first()
        if row is None:
            return None, None
        budget = float(row[0]) if row[0] is not None else None
        return budget, row[1]


def _sum_for(s, model, instance_id: int, since: datetime | None) -> dict:
    q = select(
        func.coalesce(func.sum(model.estimated_cost_usd), 0),
        func.coalesce(func.sum(model.input_tokens), 0),
        func.coalesce(func.sum(model.output_tokens), 0),
        func.count(model.id),
    ).where(model.odoo_instance_id == instance_id)
    if since is not None:
        q = q.where(model.created_at >= since)
    cost, inp, out, cnt = s.execute(q).one()
    return {
        "cost_usd": float(cost),
        "input_tokens": int(inp),
        "output_tokens": int(out),
        "count": int(cnt),
    }


def get_odoo_instance_cost(db: Database, instance_id: int) -> dict:
    """Per-instance cost: lifetime / 24h / 30d, each split analysis vs issues."""
    now = datetime.now(timezone.utc)
    windows = {
        "lifetime": None,
        "last_24h": now - timedelta(hours=24),
        "last_30d": now - timedelta(days=30),
    }
    with db.session() as s:
        out: dict = {}
        for label, since in windows.items():
            out[label] = {
                "analysis": _sum_for(s, TicketAnalysis, instance_id, since),
                "issues": _sum_for(s, TicketIssueRun, instance_id, since),
            }
    return out


def list_odoo_instances(db: Database) -> list[dict]:
    """All instances (newest first) with their cost rollup folded in."""
    with db.session() as s:
        rows = s.execute(
            select(OdooInstance).order_by(OdooInstance.created_at.desc())
        ).scalars().all()
        instances = [
            {
                "id": r.id,
                "name": r.name,
                "key_prefix": r.key_prefix,
                "callback_url": r.callback_url,
                "active": r.active,
                "daily_budget_usd": (
                    float(r.daily_budget_usd) if r.daily_budget_usd is not None else None
                ),
                "rate_limit_per_minute": r.rate_limit_per_minute,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    for inst in instances:
        inst["cost"] = get_odoo_instance_cost(db, inst["id"])
    return instances
