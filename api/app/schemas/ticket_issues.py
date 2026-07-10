"""Pydantic schemas for the create-issues endpoints (github-issues handoff)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from reva.types import Attachment


class CreateIssuesRequest(BaseModel):
    """Contract 1 payload. The field set is fixed for *required* fields by the
    shipped Odoo addon — do not add required fields (every real request would
    422). Optional additive fields are fine."""

    ticket_id: int
    model_name: str = Field(
        description='Odoo model name, e.g. "helpdesk.ticket" or "project.task"'
    )
    github_url: str = Field(description="Repository URL from the record's project")
    name: str = Field(description="Ticket/task title")
    description: str = Field(description="Plain-text ticket description (HTML stripped by Odoo)")
    analysis_html: str = Field(description='Completed REVA analysis HTML, or "" if none')
    description_docx: Attachment | None = Field(
        default=None,
        description="Consultant file (tasks only): .docx, .pdf, or .txt. When "
        "present it is THE basis for the issue split instead of "
        "description/analysis_html. (Field name fixed by the Odoo addon.)",
    )
    priority: str = Field(description='Odoo priority key, "0" (low) … "3" (urgent)')
    ticket_url: str = Field(description="Deep link back to the Odoo record")
    issue_type: Literal["BUG", "FEAT", "CR", "CONF", "DEV", "MIG", "SUP", "DOC"] | None = Field(
        default=None,
        description="Fixed work-item type for every issue of this request "
        "(Odoo wizard flow). Omitted/empty: the planner picks per issue.",
    )
    github_username: str | None = Field(
        default=None,
        description="Optional GitHub login assigned to created issues.",
    )
    github_project_url: str | None = Field(
        default=None,
        description="Optional Projects v2 board URL "
        "(https://github.com/orgs/{org}/projects/{n}); every created issue "
        "and the parent epic are added to it.",
    )
    plan_date: date | None = Field(
        default=None,
        description="Optional planned date (YYYY-MM-DD) set as the board's "
        "'Plan date' field on every added item.",
    )

    @field_validator("issue_type", mode="before")
    @classmethod
    def _empty_type_is_none(cls, v: object) -> object:
        # The Odoo wizard's empty Selection may serialize as "" — treat as unset.
        return None if v == "" else v

    @field_validator("github_username", "github_project_url", mode="before")
    @classmethod
    def _empty_str_is_none(cls, v: object) -> object:
        return None if v == "" else v

    @field_validator("plan_date", mode="before")
    @classmethod
    def _empty_date_is_none(cls, v: object) -> object:
        # Odoo may send "" for an unset Date; coerce before date parsing.
        return None if v == "" else v


class TicketIssuesAccepted(BaseModel):
    """202 body. Odoo reads request_id and the Contract 2 callback must echo it."""

    request_id: int
    job_id: str | None
    status: str


class TicketIssueRef(BaseModel):
    """One planned/created issue in a list view. number/url are null until the
    issue exists on GitHub (a partially-created plan shows both states);
    state ("open"/"closed") is synced from GitHub issue webhooks."""

    number: int | None
    title: str
    url: str | None
    state: str | None = None
    estimate_hours: float | None = None


class TicketIssueRunSummary(BaseModel):
    """List view of a run (TUI Tickets tab). Strips plan bodies (customer
    text) and the raw inputs — only the {number, title, url} refs go out."""

    id: int
    ticket_id: int
    model_name: str
    github_url: str
    status: str
    issue_type: str | None = None
    github_username: str | None = None
    # Projects v2 board + planned date the request carried (null for legacy runs).
    github_project_url: str | None = None
    plan_date: date | None = None
    issues: list[TicketIssueRef]
    parent_issue: TicketIssueRef | None = None
    error_message: str | None
    model: str | None
    estimated_cost_usd: float | None
    created_at: datetime
    completed_at: datetime | None


class TicketIssueRunPage(BaseModel):
    items: list[TicketIssueRunSummary]
    total: int


class TicketIssueRunStatus(BaseModel):
    """Ops/debug view of a run. Deliberately omits description/analysis_html/
    description_docx (customer PII), mirroring how ticket-analysis status omits
    input_text. Issues are typed as refs so un-created plan items can't leak
    their body/acceptance_criteria (Claude-rendered customer text) here."""

    id: int
    job_id: str | None
    ticket_id: int
    model_name: str
    github_url: str
    status: str
    github_project_url: str | None = None
    plan_date: date | None = None
    issues: list[TicketIssueRef] | None
    error_message: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: float | None
    created_at: datetime
    completed_at: datetime | None
