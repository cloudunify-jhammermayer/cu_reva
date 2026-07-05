"""Pydantic schemas for timesheet wording review endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from reva.types import TimesheetLine


class TimesheetReviewRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    flagged_words: list[str] = Field(
        default_factory=list,
        max_length=500,
    )
    lines: list[TimesheetLine] = Field(min_length=1, max_length=5000)


class TimesheetReviewCreated(BaseModel):
    run_id: int
    job_id: str | None
    status: str


class TimesheetReviewStatus(BaseModel):
    id: int
    job_id: str | None
    odoo_instance_id: int | None
    request_id: str
    status: str
    total_lines: int
    ok_count: int
    rewritten_count: int
    needs_human_count: int
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: float | None
    callback_sent_at: datetime | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None


class TimesheetReviewSummary(BaseModel):
    id: int
    request_id: str
    status: str
    total_lines: int
    ok_count: int
    rewritten_count: int
    needs_human_count: int
    estimated_cost_usd: float | None
    callback_sent_at: datetime | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None


class TimesheetReviewPage(BaseModel):
    items: list[TimesheetReviewSummary]
    total: int
