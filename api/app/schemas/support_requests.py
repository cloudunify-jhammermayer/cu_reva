"""Pydantic schemas for the support-answer endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from reva.types import Attachment, ChatterEntry, ImageAttachment


class SupportRequestBody(BaseModel):
    ticket_id: int
    model_name: str = Field(
        description='Odoo model name, e.g. "helpdesk.ticket" or "project.task"'
    )
    field_name: str = Field(
        description="Field on the Odoo record where the draft answer is written"
    )
    thread_id: int | None = Field(
        default=None,
        description="REVA thread id, echoed back on follow-up turns. Null on the "
        "first turn; REVA resolves or creates the thread from the record.",
    )
    subject: str = Field(default="", description="Ticket subject line")
    question: str = Field(
        description="The question to answer. On the first turn this is normally "
        "the ticket description."
    )
    github_url: str | None = Field(
        default=None,
        description="Repository URL from the record's project "
        "(https://github.com/{owner}/{repo}). Odoo requires a linked project, "
        "but REVA degrades rather than refusing: without it the answer is "
        "grounded in Odoo core knowledge only, with no project-code grounding.",
    )
    persona_context: str | None = Field(
        default=None,
        description="Consultant-authored tone/context for this record. Additive "
        "on top of the configured persona; never overrides its structured knobs.",
    )
    chatter: list[ChatterEntry] = Field(
        default_factory=list,
        description="The ticket's chatter, oldest first. `visibility` is "
        "load-bearing: entries marked internal are given to REVA as context but "
        "are never quoted or revealed in the drafted answer.",
    )
    attachment: Attachment | None = Field(
        default=None,
        description="Optional .docx/.pdf/.txt/.md file; its text is extracted "
        "and folded into the prompt alongside `question`",
    )
    images: list[ImageAttachment] = Field(
        default_factory=list,
        description="Screenshots embedded in the ticket description, in document "
        "order. Each `label` (\"Image 1\") must match the [Image N] marker the "
        "sender left in `question` where the image was. png/jpeg/gif/webp; max 6 "
        "images, 5 MB each, 8 MB total. Defaults to empty, so a sender that does "
        "not extract images is unaffected.",
    )

    @field_validator("github_url", mode="before")
    @classmethod
    def _empty_str_is_none(cls, v: object) -> object:
        # Odoo sends "" for an unset Char field; treat it as unset.
        return None if v == "" else v


class SupportRequestCreated(BaseModel):
    thread_id: int
    turn_id: int
    job_id: str | None
    status: str


class SupportTurnStatus(BaseModel):
    id: int
    thread_id: int
    seq: int
    job_id: str | None
    question: str
    answer_html: str | None
    request_kind: str | None
    answer_status: str | None
    grounding_level: str | None
    image_count: int = 0
    status: str
    error_message: str | None
    estimated_cost_usd: float | None
    created_at: datetime
    completed_at: datetime | None
    callback_sent_at: datetime | None
    callback_error: str | None

    model_config = {"from_attributes": True}


class SupportThreadSummary(BaseModel):
    id: int
    odoo_instance_id: int | None
    ticket_id: int
    model_name: str
    field_name: str
    github_url: str | None
    status: str
    created_at: datetime
    last_turn_at: datetime | None

    model_config = {"from_attributes": True}


class SupportThreadDetail(SupportThreadSummary):
    """A thread plus its turns — the drill-down payload.

    The list endpoint deliberately omits turns (a page of 50 threads would drag
    every answer body with it); this is the per-thread fetch.
    """

    turns: list[SupportTurnStatus]


class SupportThreadPage(BaseModel):
    items: list[SupportThreadSummary]
    total: int
