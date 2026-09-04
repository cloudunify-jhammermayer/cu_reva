"""Pydantic schemas for the release-log lookup endpoints (spec 2026-09-04, R2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from reva.release_log import is_safe_slug, release_slug


class ReleaseNoteRequest(BaseModel):
    """What `cu_release._reva_release_note_payload` sends. Only `release_id`
    and `name` drive the lookup; `date`, `model_name` and `task_ids` are
    accepted so the shipped Odoo payload validates, and ignored."""

    release_id: int
    name: str = Field(description="Release name; its slug is the docs/releases/<slug>.html stem")
    date: str | None = Field(
        default=None, description='"YYYY-MM-DD HH:MM:SS" (UTC) or null; not used'
    )
    model_name: str = "project.task"
    task_ids: list[int] = Field(default_factory=list)
    github_url: str | None = Field(
        default=None,
        description="The release's project repository (https://github.com/{owner}/{repo}); "
        "when present REVA reads docs/releases/<slug>.html there instead of scanning "
        "the repos mapped to the instance.",
    )

    @field_validator("github_url", mode="before")
    @classmethod
    def _empty_url_is_none(cls, v: object) -> object:
        # Odoo sends "" for a project without a repository; treat as unset.
        return None if v == "" else v

    @field_validator("name")
    @classmethod
    def _name_is_a_page_stem(cls, v: str) -> str:
        # A blank name has no page to look up; a name whose slug carries path
        # separators or a leading dot could walk out of docs/releases/. Both
        # 422 here, which reaches the Odoo user as a UserError and rolls the
        # release's pending state back.
        if not v.strip():
            raise ValueError("name must not be blank")
        if not is_safe_slug(release_slug(v)):
            raise ValueError("name must not contain path separators or start with a dot")
        return v


class ReleaseNoteCreated(BaseModel):
    """202 body. Odoo stores note_id and echoes it on the callback."""

    note_id: int
    job_id: str | None
    status: str


class ReleaseNoteSummary(BaseModel):
    id: int
    odoo_instance_id: int
    release_id: int
    release_name: str
    slug: str
    status: str
    source_repo_id: int | None
    source_path: str | None
    url: str | None
    error: str | None
    callback_sent_at: datetime | None
    created_at: datetime
    completed_at: datetime | None


class ReleaseNotePage(BaseModel):
    items: list[ReleaseNoteSummary]
    total: int
