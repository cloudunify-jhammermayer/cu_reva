"""Pydantic schemas for the odoo-instances management endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class OdooInstanceCreate(BaseModel):
    name: str
    callback_url: str = ""
    callback_api_key: str = ""  # plaintext outbound key; encrypted before storage


class OdooInstanceUpdate(BaseModel):
    name: str | None = None
    callback_url: str | None = None
    callback_api_key: str | None = None  # plaintext; re-encrypted when present
    active: bool | None = None


class TaskCost(BaseModel):
    cost_usd: float
    input_tokens: int
    output_tokens: int
    count: int


class WindowCost(BaseModel):
    analysis: TaskCost
    issues: TaskCost


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
