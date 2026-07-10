"""Pydantic schemas for the ticket-journey endpoint."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class JourneyEvent(BaseModel):
    ts: datetime | None
    kind: str
    summary: str


class JourneyTicket(BaseModel):
    odoo_instance_id: int | None
    model_name: str
    ticket_id: int
    ready: bool


class TicketJourney(BaseModel):
    ticket: JourneyTicket
    events: list[JourneyEvent]
