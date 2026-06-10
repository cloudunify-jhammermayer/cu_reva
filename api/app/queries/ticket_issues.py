"""Read queries for the ticket_issue_runs list endpoint."""

from __future__ import annotations

from sqlalchemy import func, select

from reva.db.engine import Database
from reva.db.models import TicketIssueRun


def list_ticket_issue_runs(
    db: Database,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return (items, total) for the ticket_issue_runs list view.

    Issue items are stripped to {number, title, url} — the stored plan also
    carries un-created bodies (customer-derived text) that list consumers
    (the TUI) must not receive.
    """
    with db.session() as s:
        base = select(TicketIssueRun)
        count_q = select(func.count()).select_from(TicketIssueRun)
        if status:
            base = base.where(TicketIssueRun.status == status)
            count_q = count_q.where(TicketIssueRun.status == status)

        total = s.execute(count_q).scalar_one()
        rows = s.execute(
            base.order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars().all()

        items = [
            {
                "id": r.id,
                "ticket_id": r.ticket_id,
                "model_name": r.model_name,
                "github_url": r.github_url,
                "status": r.status,
                "issues": [
                    {
                        "number": i.get("number"),
                        "title": i.get("title", ""),
                        "url": i.get("url"),
                        "state": i.get("state"),
                    }
                    for i in (r.issues or [])
                ],
                "error_message": r.error_message,
                "model": r.model,
                "estimated_cost_usd": (
                    float(r.estimated_cost_usd) if r.estimated_cost_usd else None
                ),
                "created_at": r.created_at,
                "completed_at": r.completed_at,
            }
            for r in rows
        ]
    return items, total
