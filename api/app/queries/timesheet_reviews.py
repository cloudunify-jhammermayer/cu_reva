"""Read queries for timesheet wording review endpoints."""

from __future__ import annotations

from sqlalchemy import func, select

from reva.db.engine import Database
from reva.db.models import TimesheetReviewRun


def list_timesheet_reviews(
    db: Database,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    with db.session() as s:
        base = select(TimesheetReviewRun)
        count_q = select(func.count()).select_from(TimesheetReviewRun)
        if status:
            base = base.where(TimesheetReviewRun.status == status)
            count_q = count_q.where(TimesheetReviewRun.status == status)

        total = s.execute(count_q).scalar_one()
        rows = s.execute(
            base.order_by(TimesheetReviewRun.created_at.desc()).limit(limit).offset(offset)
        ).scalars().all()

        items = [
            {
                "id": r.id,
                "request_id": r.request_id,
                "status": r.status,
                "total_lines": r.total_lines,
                "ok_count": r.ok_count,
                "rewritten_count": r.rewritten_count,
                "needs_human_count": r.needs_human_count,
                "estimated_cost_usd": (
                    float(r.estimated_cost_usd) if r.estimated_cost_usd else None
                ),
                "callback_sent_at": r.callback_sent_at,
                "error_message": r.error_message,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
            }
            for r in rows
        ]
    return items, total
