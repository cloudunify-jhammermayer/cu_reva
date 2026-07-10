"""Pydantic response schemas for review-related endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class FindingSummary(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    severity: str
    category: str
    title: str
    confidence: float | None
    file_path: str | None
    line_start: int | None
    # Populated by the findings list (joins review_run + repository); left None on
    # the review-detail path, which already knows its repo/PR.
    repo_full_name: str | None = None
    pr_number: int | None = None


class FindingDetail(FindingSummary):
    body: str
    suggestion: str | None
    is_odoo_specific: bool
    thumbs_up: int
    thumbs_down: int


class IntentCheckItem(BaseModel):
    issue_number: int
    verdict: str
    note: str = ""


class ReviewSummary(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    repo_full_name: str
    pr_number: int
    pr_title: str
    author_login: str | None
    head_sha: str
    status: str
    review_mode: str
    model: str | None
    risk_level: str | None
    finding_count: int
    duration_ms: int | None
    estimated_cost_usd: float | None
    created_at: datetime


class ReviewDetail(ReviewSummary):
    summary: str | None
    decline_reason: str | None
    error_message: str | None
    error_class: str | None
    input_tokens: int | None
    output_tokens: int | None
    findings: list[FindingDetail]
    intent_check: list[IntentCheckItem] | None = None


class ReviewPage(BaseModel):
    items: list[ReviewSummary]
    total: int


class FailurePage(BaseModel):
    items: list[ReviewDetail]
    total: int


class FindingPage(BaseModel):
    items: list[FindingSummary]
    total: int


class PendingReview(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    repo_full_name: str
    pr_number: int
    pr_title: str
    head_sha: str
    scheduled_at: datetime
    trigger_event: str
    review_mode: str
    status: str = "pending"


class PendingPage(BaseModel):
    items: list[PendingReview]
    total: int
