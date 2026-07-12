"""Pydantic schemas for ticket analysis endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from reva.types import Attachment


class TicketAnalysisRequest(BaseModel):
    ticket_id: int
    model_name: str = Field(
        description='Odoo model name, e.g. "helpdesk.ticket" or "project.task"'
    )
    field_name: str = Field(
        description="Field on the Odoo record where the result will be written"
    )
    text: str = Field(description="Ticket description text to analyse")
    attachment: Attachment | None = Field(
        default=None,
        description="Optional .docx/.pdf/.txt file; its text is extracted and "
        "folded into the analysis prompt alongside `text`",
    )
    github_url: str | None = Field(
        default=None,
        description="Optional repository URL from the record's project "
        "(https://github.com/{owner}/{repo}). Persisted for dashboard repo "
        "grouping (TUI Tickets tab); format-validated at accept time, no "
        "reachability check.",
    )

    @field_validator("github_url", mode="before")
    @classmethod
    def _empty_str_is_none(cls, v: object) -> object:
        # Odoo sends "" for an unset Char field; treat it as unset.
        return None if v == "" else v


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
    github_url: str | None = None
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
    github_url: str | None = None
    status: str
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: float | None
    created_at: datetime
    completed_at: datetime | None
    error_message: str | None = None
    # Delivery visibility: callback_sent_at is null until the Odoo write_field
    # callback lands; a completed row with a null value never reached Odoo.
    callback_sent_at: datetime | None = None
    callback_error: str | None = None
    # Dev-time estimate summed over result_structured.estimates (null when absent).
    estimate_hours_min: float | None = None
    estimate_hours_max: float | None = None
    odoo_instance_id: int | None = None


class TicketAnalysisPage(BaseModel):
    items: list[TicketAnalysisSummary]
    total: int
