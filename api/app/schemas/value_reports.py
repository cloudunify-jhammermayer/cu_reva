"""Schemas for monthly value reports."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ValueReportEntry(BaseModel):
    id: int
    period_start: datetime
    period_end: datetime
    content_md: str
    stats: dict
    chat_sent: bool
    created_at: datetime


class ValueReportPage(BaseModel):
    items: list[ValueReportEntry]
    total: int
