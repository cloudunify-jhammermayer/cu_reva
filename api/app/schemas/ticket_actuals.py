"""Pydantic schemas for the ticket-actuals endpoint (Odoo -> REVA push)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TicketActualsRequest(BaseModel):
    """Timesheet totals for a ticket, pushed by Odoo when it is marked done
    (the manual deployment step). Fire-and-forget from Odoo's side: no
    callback echoes it. A re-done ticket re-sends its totals — latest wins."""

    ticket_id: int
    model_name: str = Field(
        description='Odoo model name, e.g. "helpdesk.ticket" or "project.task"'
    )
    actual_hours: float = Field(
        ge=0, description="Total timesheet hours booked on the ticket at done-time"
    )
    timesheet_line_count: int | None = Field(
        default=None, ge=0, description="Number of timesheet lines behind the total"
    )


class TicketActualsRecorded(BaseModel):
    """200 body for ticket-actuals."""

    status: str
