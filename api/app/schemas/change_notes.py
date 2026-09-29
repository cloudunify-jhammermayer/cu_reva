"""Schemas for the change-notes list endpoint."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ChangeNoteSummary(BaseModel):
    """List view of a change note (TUI Tickets tab). No note_html — the drafted
    text stays out of this feed. modules/submodules: None = never looked up."""

    id: int
    repo_full_name: str
    pr_number: int
    pr_title: str | None = None
    pr_url: str | None = None
    odoo_instance_id: int
    ticket_id: int
    model_name: str
    status: str
    source: str
    modules: list[str] | None = None
    submodules: list[str] | None = None
    error_message: str | None = None
    estimated_cost_usd: float | None = None
    created_at: datetime
    completed_at: datetime | None = None
    delivered_at: datetime | None = None


class ChangeNotePage(BaseModel):
    items: list[ChangeNoteSummary]
    total: int
