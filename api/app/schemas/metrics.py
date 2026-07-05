"""Pydantic response schemas for metrics endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PeriodStats(BaseModel):
    reviews_completed: int
    reviews_failed: int
    success_rate: float
    avg_duration_ms: float | None


class FindingCounts(BaseModel):
    critical: int
    major: int
    minor: int
    info: int


class DashboardMetrics(BaseModel):
    last_24h: PeriodStats
    last_7d: PeriodStats
    findings_24h: FindingCounts
    total_cost_7d: float
    avg_cost_per_review_7d: float | None
    active_workers: int = 0
    degradations_24h: int = 0
    core_knowledge: list["CoreVersionStatus"] = Field(default_factory=list)


class CoreVersionStatus(BaseModel):
    odoo_version: str
    loaded_at: datetime
    modules: int
    sections: int


class DeveloperStat(BaseModel):
    author_login: str
    review_count: int
    avg_findings: float
    avg_major_critical: float
    trend: str  # improving | stable | worsening


class CostEntry(BaseModel):
    repo_full_name: str
    period: str
    total_cost_usd: float
    review_count: int


class FeedbackEntry(BaseModel):
    category: str
    severity: str
    thumbs_up: int
    thumbs_down: int
    approval_rate: float | None


class LearningStat(BaseModel):
    repo: str
    category: str
    findings: int
    dismissed: int
    resolved_by_fix: int
    still_open_at_merge: int


class MuteEntry(BaseModel):
    repo: str
    category: str
    muted_by: str
    created_at: datetime


class LearnedMemoryEntry(BaseModel):
    repo: str
    version: int
    content: str
    item_count: int
    estimated_cost_usd: float | None = None
    created_at: datetime
