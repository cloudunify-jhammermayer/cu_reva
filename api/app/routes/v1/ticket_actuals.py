"""POST /api/v1/ticket-actuals — Odoo pushes per-ticket timesheet totals when
a ticket is marked done (estimate-calibration loop C1). Synchronous DB upsert,
no job: nothing here touches GitHub or makes a paid Claude call."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request

from app.dependencies import ResolvedOdooInstance, get_db, require_odoo_instance
from app.schemas.ticket_actuals import TicketActualsRecorded, TicketActualsRequest
from reva.db import writers
from reva.db.engine import Database

create_router = APIRouter()  # instance-key gated (see routes/v1/__init__.py)
logger = structlog.get_logger()


@create_router.post("/ticket-actuals", response_model=TicketActualsRecorded)
def record_ticket_actuals(
    body: TicketActualsRequest,
    request: Request,
    db: Database = Depends(get_db),
    instance: ResolvedOdooInstance = Depends(require_odoo_instance),
) -> dict:
    """Record the totals. Tickets without REVA-created issues are accepted
    too — the instance is authenticated and its data trusted; calibration
    simply has nothing to compare those against until issues exist."""
    writers.record_ticket_actuals(
        db,
        odoo_instance_id=instance.id,
        ticket_id=body.ticket_id,
        model_name=body.model_name,
        actual_hours=body.actual_hours,
        timesheet_line_count=body.timesheet_line_count,
    )
    logger.info(
        "ticket_actuals_recorded",
        instance_id=instance.id,
        ticket_id=body.ticket_id,
        model_name=body.model_name,
        actual_hours=body.actual_hours,
    )
    return {"status": "recorded"}
