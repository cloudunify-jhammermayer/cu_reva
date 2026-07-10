"""GET /api/v1/ticket-journeys — read-only per-ticket timeline."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_db
from app.queries import ticket_journeys as q
from app.schemas.ticket_journeys import TicketJourney
from reva.db.engine import Database

router = APIRouter()


@router.get("/ticket-journeys", response_model=TicketJourney)
def get_ticket_journey(
    model_name: str,
    ticket_id: int,
    odoo_instance_id: int | None = None,
    db: Database = Depends(get_db),
) -> TicketJourney:
    data = q.get_ticket_journey(db, odoo_instance_id, model_name, ticket_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Ticket has no REVA activity")
    return TicketJourney.model_validate(data)
