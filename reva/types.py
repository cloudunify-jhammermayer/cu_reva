"""Data contracts for the review worker.

Mirrors the schema in `doc/pr-review-requirements.md`. The pydantic models
here are the single source of truth — the Claude tool schema is derived
from them in `review_tool.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Literals from pr-review-requirements.md ----------------------------------

Severity = Literal["info", "minor", "major", "critical"]
Category = Literal[
    "bug",
    "security",
    "performance",
    "maintainability",
    "test",
    "docs",
    "style",
    "architecture",
    "odoo",
]
RiskLevel = Literal["low", "medium", "high", "critical"]
# "diff-all" = diff-depth review (like "diff") but over ALL changed paths, not
# just the custom_addons prefixes — triggered by the /review-all comment.
ReviewMode = Literal["diff", "full", "deep", "diff-all"]
TriggerEvent = Literal[
    "opened", "synchronize", "reopened", "ready_for_review",
    "comment", "manual", "manual_requeue",
]

ReviewStatus = Literal["completed", "stale", "declined", "failed", "skipped_trivial"]

# Per-repo Check Run blocking threshold (.claude-review.yml: block_on_severity).
# The lowest finding severity that fails the Check Run; "none" never blocks.
BlockSeverity = Literal["critical", "major", "minor", "none"]


# --- Repo config --------------------------------------------------------------


class RepoConfig(BaseModel):
    """Parsed contents of .claude-review.yml.

    Unknown keys from the YAML file are silently ignored so adding new fields
    to the config schema doesn't require updating existing repo config files.
    """

    model_config = ConfigDict(extra="ignore")

    max_diff_lines: int | None = None
    max_diff_tokens: int | None = None
    # Optional stricter caps for XML-only PRs (verbose view dumps). None = no extra
    # cap beyond the general max_diff_lines/tokens.
    max_xml_diff_lines: int | None = None
    max_xml_diff_tokens: int | None = None
    skip_paths: list[str] = Field(default_factory=list)
    # When true, review every changed path (like the /review-all command),
    # not just the custom_addons prefixes. Default keeps the custom_addons lock.
    review_all_paths: bool = False
    odoo: bool = False
    framework: str | None = None
    custom_instructions: str | None = None
    # Lowest finding severity that fails the Check Run. Default "major" keeps the
    # historical behavior (any major/critical blocks); "none" never blocks.
    block_on_severity: BlockSeverity = "major"
    # Per-repo override for the second-pass self-critique. None = defer to the
    # global REVA_VERIFY_HIGH_COST setting; True/False force it on/off.
    verify_findings: bool | None = None


# --- Finding ------------------------------------------------------------------


class Finding(BaseModel):
    """One review finding. Matches the JSON schema in pr-review-requirements.md §5."""

    severity: Severity
    category: Category
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    title: str = Field(max_length=80)
    body: str
    suggestion: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    is_odoo_specific: bool = False

    @field_validator("title", mode="before")
    @classmethod
    def _truncate_title(cls, v: object) -> object:
        if isinstance(v, str) and len(v) > 80:
            return v[:77] + "..."
        return v

    # CORR-14: bound body/suggestion so one oversized finding can't blow past
    # GitHub's comment-size limit and fail the whole review post.
    @field_validator("body", mode="before")
    @classmethod
    def _truncate_body(cls, v: object) -> object:
        if isinstance(v, str) and len(v) > 8000:
            return v[:7997] + "..."
        return v

    @field_validator("suggestion", mode="before")
    @classmethod
    def _truncate_suggestion(cls, v: object) -> object:
        if isinstance(v, str) and len(v) > 4000:
            return v[:3997] + "..."
        return v

    @model_validator(mode="after")
    def _check_line_range(self) -> "Finding":
        if (
            self.line_start is not None
            and self.line_end is not None
            and self.line_end < self.line_start
        ):
            raise ValueError("line_end must be >= line_start")
        return self


# --- Review result ------------------------------------------------------------


class ReviewResult(BaseModel):
    """Outcome of a single review attempt.

    Returned by Reviewer.execute. Persisted by tasks.run_review into
    review_runs + review_findings tables.

    Field population per status:
      completed  — all fields set; diff carries the reviewed unified diff (not persisted).
      declined   — summary + decline_reason only.
      stale      — summary only.
      failed     — error_message + error_class only.
      skipped_trivial — summary only (no findings, no Claude call, no spend).
    """

    status: ReviewStatus
    summary: str = ""
    risk_level: RiskLevel = "low"
    findings: list[Finding] = Field(default_factory=list)

    # Transient: the reviewed diff, carried from Reviewer.execute to runner._post_completed
    # for hunk parsing. Not written to the database.
    diff: str = ""

    # Transient: per-repo Check Run gating threshold, carried from Reviewer.execute
    # to compute_check_conclusion. Not persisted; the default "major" preserves
    # behavior for results built outside execute (declines/stale/failed).
    block_on_severity: BlockSeverity = "major"

    model: str | None = None
    prompt_version: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    estimated_cost_usd: float = 0.0

    decline_reason: str | None = None

    error_message: str | None = None
    error_class: Literal["transient", "permanent"] | None = None
    delta_base_sha: str | None = None   # set when this was a delta review


# --- Job parameters -----------------------------------------------------------


class JobParams(BaseModel):
    """Inputs handed to Reviewer.execute (and to the RQ task)."""

    repository_id: int
    pull_request_id: int
    head_sha: str
    installation_id: int
    review_mode: ReviewMode = "diff"
    trigger_event: TriggerEvent


# --- Claude content blocks (Anthropic Messages API shape) ---------------------


class ContentBlock(TypedDict, total=False):
    """A single content block sent to the Claude Messages API.

    `cache_control` is attached to blocks we want to cache (system prompt,
    odoo19.md, CLAUDE.md). See `prompt_builder.build_system_blocks`.
    """

    type: str
    text: str
    cache_control: dict


# --- Ticket analysis types ---------------------------------------------------


def _unwrap_json_list(v: object) -> object:
    """Claude occasionally returns list fields as JSON strings; unwrap them."""
    if isinstance(v, str):
        import json
        try:
            return json.loads(v)
        except json.JSONDecodeError as exc:
            # Stringified AND malformed (typically unescaped quotes in the
            # embedded text) — name the real problem instead of leaking a bare
            # json.loads parse error into the validation message.
            raise ValueError(
                f"list field arrived as a JSON string that does not parse "
                f"(malformed embedded JSON: {exc})"
            ) from exc
    return v


class AcceptanceCriterion(BaseModel):
    given: str
    when: str
    then: str
    confidence: Literal["explicit", "inferred", "assumed"] = "inferred"


class TicketTestCase(BaseModel):
    category: Literal["happy_path", "edge_case", "error_scenario"]
    description: str
    confidence: Literal["explicit", "inferred", "assumed"] = "inferred"


class SourcedItem(BaseModel):
    text: str
    confidence: Literal["explicit", "inferred", "assumed"] = "inferred"


class MissingInfoItem(BaseModel):
    text: str
    confidence: Literal["certain", "likely", "possible"] = "likely"


class TicketAnalysisResult(BaseModel):
    """Structured output from the ticket analysis tool_use call."""

    summary: str
    missing_info: list[MissingInfoItem] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    test_cases: list[TicketTestCase] = Field(default_factory=list)
    definition_of_ready: list[SourcedItem] = Field(default_factory=list)
    definition_of_done: list[SourcedItem] = Field(default_factory=list)
    odoo_notes: list[SourcedItem] = Field(default_factory=list)

    @field_validator(
        "missing_info", "acceptance_criteria", "test_cases",
        "definition_of_ready", "definition_of_done", "odoo_notes",
        mode="before",
    )
    @classmethod
    def _parse_json_string_list(cls, v: object) -> object:
        return _unwrap_json_list(v)


class Attachment(BaseModel):
    """A file forwarded by Odoo ({filename, content_base64}). Accepted types are
    .docx / .pdf / .txt — the filename extension is the authoritative gate (see
    reva.attachment_text). Shared by the ticket-analysis attachment and the
    create-issues description_docx; on a create-issues request it is THE basis
    for the issue split."""

    filename: str
    content_base64: str


class TicketJobParams(BaseModel):
    """Inputs handed to the ticket analysis RQ job."""

    analysis_id: int
    odoo_instance_id: int
    ticket_id: int
    model_name: str  # e.g. "helpdesk.ticket" or "project.task"
    field_name: str
    text: str
    attachment: Attachment | None = None  # optional .docx/.pdf/.txt, folded into the prompt


# --- Ticket issue creation types -----------------------------------------------


class TicketIssueItem(BaseModel):
    """One GitHub issue planned from an Odoo ticket."""

    title: str
    body: str
    acceptance_criteria: list[str] = Field(default_factory=list)

    @field_validator("title", mode="before")
    @classmethod
    def _truncate_title(cls, v: object) -> object:
        # Bound well under GitHub's 256-char issue-title limit.
        if isinstance(v, str) and len(v) > 200:
            return v[:197] + "..."
        return v

    @field_validator("acceptance_criteria", mode="before")
    @classmethod
    def _parse_json_string_list(cls, v: object) -> object:
        return _unwrap_json_list(v)


class TicketIssuePlan(BaseModel):
    """Structured output from the submit_ticket_issues tool_use call."""

    issues: list[TicketIssueItem] = Field(min_length=1, max_length=10)

    @field_validator("issues", mode="before")
    @classmethod
    def _parse_json_string_list(cls, v: object) -> object:
        return _unwrap_json_list(v)


class TicketIssueJobParams(BaseModel):
    """Inputs handed to the create-issues RQ job: the Contract 1 payload from
    the Odoo addon (github-issues handoff) plus the ticket_issue_runs row id,
    which doubles as the request_id Odoo correlates callbacks with."""

    run_id: int
    ticket_id: int
    model_name: str  # e.g. "helpdesk.ticket" or "project.task"
    github_url: str
    name: str
    description: str
    analysis_html: str  # "" when the record has no completed analysis
    description_docx: Attachment | None = None  # tasks only; .docx/.pdf/.txt
    priority: str  # Odoo priority key "0".."3"
    ticket_url: str


class AuditJobParams(BaseModel):
    """Inputs handed to the repo audit RQ job."""

    repository_id: int
    installation_id: int
    requested_by: str | None = None


class AuditResult(BaseModel):
    """Outcome of a single repo audit run."""

    status: Literal["completed", "failed"]
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    model: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int = 0
    estimated_cost_usd: float = 0.0


# --- Claude response ----------------------------------------------------------


class ClaudeResponse(BaseModel):
    """Normalized response from ClaudeClient.review.

    `tool_use_input` is the parsed input to the `submit_review` tool call.
    If Claude did not produce a tool_use block, this is None and the caller
    must treat the response as a permanent failure.
    """

    model: str
    stop_reason: str | None = None
    tool_use_input: dict | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    # Authoritative cost reported by the Claude Code CLI (`total_cost_usd`).
    # 0.0 on the Messages-API path, where cost is derived from token counts.
    total_cost_usd: float = 0.0
