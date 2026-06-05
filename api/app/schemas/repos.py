"""Pydantic response schemas for repository endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class RepoSummary(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    full_name: str
    owner: str
    name: str
    default_branch: str | None
    installation_id: int
    enabled: bool
    review_count: int
    last_review_at: datetime | None
    created_at: datetime


class RepoPage(BaseModel):
    items: list[RepoSummary]
    total: int


class AddRepoRequest(BaseModel):
    owner: str
    name: str
