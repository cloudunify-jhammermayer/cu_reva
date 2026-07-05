"""Pydantic schemas for the ops-event endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class OpsEventEntry(BaseModel):
    id: int
    component: str
    severity: str
    event: str
    detail: dict | None
    created_at: datetime


class OpsEventPage(BaseModel):
    items: list[OpsEventEntry]
    total: int
