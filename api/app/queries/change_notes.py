"""Read queries for the change_notes list endpoint."""

from __future__ import annotations

from sqlalchemy import func, select

from reva.db.engine import Database
from reva.db.models import ChangeNote


def list_change_notes(
    db: Database,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return (items, total) for the change_notes list view.

    note_html is deliberately not selected — the note text stays out of the feed.
    """
    with db.session() as s:
        total = s.execute(select(func.count()).select_from(ChangeNote)).scalar_one()
        rows = s.execute(
            select(ChangeNote)
            .order_by(ChangeNote.created_at.desc(), ChangeNote.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars().all()

        items = [
            {
                "id": r.id,
                "repo_full_name": r.repo_full_name,
                "pr_number": r.pr_number,
                "pr_title": r.pr_title,
                "pr_url": r.pr_url,
                "odoo_instance_id": r.odoo_instance_id,
                "ticket_id": r.ticket_id,
                "model_name": r.model_name,
                "status": r.status,
                "source": r.source,
                "modules": r.modules,
                "submodules": r.submodules,
                "error_message": r.error_message,
                "estimated_cost_usd": (
                    float(r.estimated_cost_usd) if r.estimated_cost_usd is not None else None
                ),
                "created_at": r.created_at,
                "completed_at": r.completed_at,
                "delivered_at": r.delivered_at,
            }
            for r in rows
        ]
    return items, total
