"""Pydantic schemas for the odoo-instances management endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class OdooInstanceCreate(BaseModel):
    name: str
    callback_url: str = ""
    callback_api_key: str = ""  # plaintext outbound key; encrypted before storage
    odoo_version: str | None = None


class OdooInstanceUpdate(BaseModel):
    name: str | None = None
    callback_url: str | None = None
    callback_api_key: str | None = None  # plaintext; re-encrypted when present
    active: bool | None = None
    daily_budget_usd: float | None = Field(default=None, ge=0)
    rate_limit_per_minute: int | None = Field(default=None, ge=1)
    odoo_version: str | None = None


class TaskCost(BaseModel):
    cost_usd: float
    input_tokens: int
    output_tokens: int
    count: int


class WindowCost(BaseModel):
    analysis: TaskCost
    issues: TaskCost
    # Defaulted so pre-timesheet payloads/fixtures still validate.
    timesheets: TaskCost = TaskCost(cost_usd=0.0, input_tokens=0,
                                    output_tokens=0, count=0)


class OdooInstanceCost(BaseModel):
    lifetime: WindowCost
    last_24h: WindowCost
    last_30d: WindowCost


class OdooInstanceSummary(BaseModel):
    id: int
    name: str
    key_prefix: str
    callback_url: str
    active: bool
    daily_budget_usd: float | None = None
    rate_limit_per_minute: int | None = None
    odoo_version: str | None = None
    created_at: datetime
    cost: OdooInstanceCost


class OdooInstancePage(BaseModel):
    items: list[OdooInstanceSummary]
    total: int


class OdooInstanceCreated(BaseModel):
    """Returned ONCE on create/rotate — carries the plaintext inbound key."""

    id: int
    name: str
    key_prefix: str
    api_key: str  # plaintext, shown once
