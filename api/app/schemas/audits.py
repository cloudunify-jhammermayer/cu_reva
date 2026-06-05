"""Pydantic response schemas for audit endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class AuditFindingSummary(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    audit_run_id: int
    repo_full_name: str
    severity: str
    category: str
    title: str
    confidence: float | None
    file_path: str | None
    line_start: int | None
    github_issue_number: int | None
    created_at: datetime


class AuditFindingPage(BaseModel):
    items: list[AuditFindingSummary]
    total: int


class AuditRunSummary(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    repo_full_name: str
    status: str
    model: str | None
    finding_count: int
    issued_count: int
    duration_ms: int | None
    requested_by: str | None
    created_at: datetime
    completed_at: datetime | None


class AuditRunPage(BaseModel):
    items: list[AuditRunSummary]
    total: int
