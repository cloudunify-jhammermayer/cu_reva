"""Read queries for ticket_analyses endpoints."""

from __future__ import annotations

from sqlalchemy import func, select

from reva.db.engine import Database
from reva.db.models import TicketAnalysis


def list_ticket_analyses(
    db: Database,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return (items, total) for the ticket_analyses list view."""
    with db.session() as s:
        base = select(TicketAnalysis)
        count_q = select(func.count()).select_from(TicketAnalysis)
        if status:
            base = base.where(TicketAnalysis.status == status)
            count_q = count_q.where(TicketAnalysis.status == status)

        total = s.execute(count_q).scalar_one()
        rows = s.execute(
            base.order_by(TicketAnalysis.created_at.desc()).limit(limit).offset(offset)
        ).scalars().all()

        items = []
        for r in rows:
            est_min, est_max = _estimate_sums(r.result_structured)
            items.append(
                {
                    "id": r.id,
                    "ticket_id": r.ticket_id,
                    "model_name": r.model_name,
                    "field_name": r.field_name,
                    "status": r.status,
                    "model": r.model,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "estimated_cost_usd": (
                        float(r.estimated_cost_usd) if r.estimated_cost_usd else None
                    ),
                    "created_at": r.created_at,
                    "completed_at": r.completed_at,
                    "error_message": r.error_message,
                    "callback_sent_at": r.callback_sent_at,
                    "callback_error": r.callback_error,
                    "estimate_hours_min": est_min,
                    "estimate_hours_max": est_max,
                    "odoo_instance_id": r.odoo_instance_id,
                }
            )
    return items, total


def _estimate_sums(structured: object) -> tuple[float | None, float | None]:
    """Sum the per-story dev-time estimates in a stored result_structured blob.

    Returns (None, None) when the analysis has no estimates or the blob is
    shaped unexpectedly (older rows predate the field)."""
    if not isinstance(structured, dict):
        return None, None
    estimates = structured.get("estimates")
    if not estimates:
        return None, None
    try:
        est_min = sum(float(e["min_hours"]) for e in estimates)
        est_max = sum(float(e["max_hours"]) for e in estimates)
    except (KeyError, TypeError, ValueError):
        return None, None
    return est_min, est_max
