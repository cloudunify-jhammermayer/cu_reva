"""Pydantic schemas for ticket analysis endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TicketAnalysisRequest(BaseModel):
    ticket_id: int
    model_name: str = Field(
        description='Odoo model name, e.g. "helpdesk.ticket" or "project.task"'
    )
    field_name: str = Field(
        description="Field on the Odoo record where the result will be written"
    )
    text: str = Field(description="Ticket description text to analyse")


class TicketAnalysisCreated(BaseModel):
    analysis_id: int
    job_id: str | None
    status: str


class TicketAnalysisStatus(BaseModel):
    id: int
    job_id: str | None
    ticket_id: int
    model_name: str
    field_name: str
    status: str
    result_html: str | None
    error_message: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: float | None
    created_at: datetime
    completed_at: datetime | None


class TicketAnalysisSummary(BaseModel):
    id: int
    ticket_id: int
    model_name: str
    field_name: str
    status: str
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: float | None
    created_at: datetime
    completed_at: datetime | None


class TicketAnalysisPage(BaseModel):
    items: list[TicketAnalysisSummary]
    total: int
