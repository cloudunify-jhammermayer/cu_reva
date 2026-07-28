"""Data contracts for the review worker.

Mirrors the schema in `doc/pr-review-requirements.md`. The pydantic models
here are the single source of truth — the Claude tool schema is derived
from them in `review_tool.py`.
"""

from __future__ import annotations

from datetime import date, datetime
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
    "standard-functionality",
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
    # Kill switch for per-repo learned memory (Tier 3 B): false disables both
    # injecting the learned block and distilling new versions for this repo.
    learned_memory: bool = True
    # Kill switch for cross-branch review reuse (dev→stage→prod promotions).
    cross_branch_reuse: bool = True
    # Per-repo brake on planner-gated CLI code grounding (ticket analyses and
    # support answers). Global REVA_TICKET_CODE_GROUNDING must also be true.
    # False keeps this repo's answers on the cheap docs-only path.
    code_grounding: bool = True
    # Kill switch for the default-off triage pre-pass. Global
    # REVA_TRIAGE_ENABLED must also be true.
    triage: bool = True
    # Kill switch for GitHub security-alert context (scanner-feed spec).
    scanner_feed: bool = True
    ticket_grounding: bool = True
    change_notes: bool = True
    # Kill switch for the issue-conformance verdict (Requirements check):
    # false skips the GraphQL link lookup and drops any returned verdicts.
    # The plain stated_intent context injection is unaffected.
    intent_check: bool = True
    # Kill switch for GitHub Projects board Status sync (linked-PR legs):
    # false stops REVA moving cards to "In Progress"/"In review" for this repo.
    board_status_sync: bool = True
    # Kill switch for per-issue work-status callbacks to Odoo (in_progress /
    # in_review), independent of board_status_sync: false stops REVA sending
    # work-status hints for this repo while the board keeps moving (or vice versa).
    work_status: bool = True
    # Which /core version this repo's reviews + ticket analyses consult, e.g.
    # "19.0". Defaults to the org baseline "19.0"; set it explicitly for a repo
    # on another version (e.g. "17.0"), or to null in .claude-review.yml to
    # disable core-knowledge grounding for this repo.
    odoo_version: str | None = "19.0"

    @field_validator("odoo_version", mode="before")
    @classmethod
    def _normalize_odoo_version(cls, v: object) -> object:
        # YAML parses an unquoted `17.0` as a float and `19` as an int. Without
        # this, a numeric value fails str-validation and load_repo_config falls
        # back to the "19.0" default — silently grounding the WRONG version.
        # Coerce numbers to the "<major>.0" string form. Quoting is still
        # recommended, and required for suffixes like "saas~17.4".
        if isinstance(v, bool):  # bool is an int subclass — leave it to fail
            return v
        if isinstance(v, (int, float)):
            s = str(v)
            return s if "." in s else f"{s}.0"
        return v


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


# --- Intent issue verdict -----------------------------------------------------

IntentVerdict = Literal["matches", "partial", "does_not_match", "unclear"]


class IntentIssueVerdict(BaseModel):
    """Per-linked-issue conformance verdict (issue-conformance spec 2026-07-10).

    Advisory only: rendered as a "Requirements check" section and persisted,
    but never feeds compute_check_conclusion — the verdict derives from
    UNTRUSTED issue text.
    """

    issue_number: int
    verdict: IntentVerdict
    note: str = ""

    @field_validator("note", mode="before")
    @classmethod
    def _truncate_note(cls, v: object) -> object:
        if isinstance(v, str) and len(v) > 300:
            return v[:297] + "..."
        return v


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
    # Per-linked-issue conformance verdicts (None = no linked issues, delta
    # review, repo opted out, or the model omitted them). Persisted as JSON.
    intent_check: list[IntentIssueVerdict] | None = None

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
    # Cross-branch reuse fingerprint (full-scope reviews only; NULL on delta runs).
    diff_hash: str | None = None
    # Set on a carried-forward run: the review_runs.id whose verdict was reused.
    carried_from_run_id: int | None = None
    # Learned-memory version injected into this review's prompt (Tier 3 B), or
    # None when none was active/allowed. Runner stamps it onto the run row.
    learned_memory_version: int | None = None
    # Set when the triage pre-pass upgraded this run's effective review depth.
    triage_escalation: str | None = None


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

    `cache_control` is attached to blocks we want to cache. Used by the
    Messages-API paths (ticket analysis, comment replies, the finding verifier).
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


# Fragments of the XML-style tool-call format Claude uses in agentic harnesses.
# A model that falls back to it while a tool schema is in force has nowhere to
# put that text but inside the string field it is currently filling, so the
# whole "tool call" lands in one field and the rest come back empty — a
# schema-VALID result that renders as literal `<parameter name=…>` markup in
# Odoo (analysis 80 / ticket 130, 2026-07-27; the support path saw the same
# thing in `answer`, prompts CHANGELOG v2.13). Nothing else inspects string
# content, so this is the only place that can catch it.
_TOOL_CALL_SYNTAX = (
    "<parameter name=",
    "</parameter>",
    "<invoke name=",
    "</invoke>",
    "</antml",
)


def _reject_tool_call_syntax(v: object) -> object:
    """Refuse a text field that carries tool-call syntax.

    Guards the FIRST free-text field of each model — the one the degeneration
    has to land in, since every field before it is an enum. Deliberately narrow:
    tickets legitimately discuss XML views and `<field>` tags, so only the
    tool-call markers count.
    """
    if isinstance(v, str):
        for marker in _TOOL_CALL_SYNTAX:
            if marker in v:
                raise ValueError(
                    f"contains tool-call syntax ({marker!r}) — the model wrote its "
                    f"tool call as text instead of calling the tool, so this field "
                    f"holds the whole output and the others are empty"
                )
    return v


class SourcedItem(BaseModel):
    text: str
    confidence: Literal["explicit", "inferred", "assumed"] = "inferred"


class MissingInfoItem(BaseModel):
    text: str
    confidence: Literal["certain", "likely", "possible"] = "likely"


class CoverageFeature(BaseModel):
    """One stock Odoo capability that covers part of a ticket."""

    name: str
    module: str = ""
    kind: Literal["app", "setting", "feature"] = "feature"
    how: str = ""
    reference: str = ""
    confidence: Literal["high", "medium", "low"] = "medium"


class StandardCoverage(BaseModel):
    """Build-vs-configure verdict for a ticket."""

    coverage: Literal["full", "partial", "none", "unknown"] = "unknown"
    features: list[CoverageFeature] = Field(default_factory=list)
    notes: str = ""

    @field_validator("features", mode="before")
    @classmethod
    def _parse_json_string_list(cls, v: object) -> object:
        return _unwrap_json_list(v)


class CustomizationFeature(BaseModel):
    """One documented existing customization in the customer repo that a ticket
    already touches or is covered by."""

    name: str
    addon: str = ""             # the custom addon the repo docs attribute it to
    how: str = ""               # what it does / how it relates to the request
    reference: str = ""         # retrieved doc path#anchor
    confidence: Literal["high", "medium", "low"] = "medium"


class ExistingCustomizations(BaseModel):
    """Whether the customer's own customizations already cover/touch a ticket,
    grounded in the repo's project documentation."""

    coverage: Literal["full", "partial", "none", "unknown"] = "unknown"
    features: list[CustomizationFeature] = Field(default_factory=list)
    notes: str = ""

    @field_validator("features", mode="before")
    @classmethod
    def _parse_json_string_list(cls, v: object) -> object:
        return _unwrap_json_list(v)


class StoryEstimate(BaseModel):
    """Development-time estimate for one user story split out of a ticket."""

    story: str                      # one-sentence user story
    kind: Literal["custom_dev", "configuration", "mixed"] = "custom_dev"
    min_hours: float
    max_hours: float
    confidence: Literal["high", "medium", "low"] = "medium"
    assumptions: list[str] = Field(default_factory=list)

    @field_validator("assumptions", mode="before")
    @classmethod
    def _parse_json_string_list(cls, v: object) -> object:
        return _unwrap_json_list(v)


class TicketAnalysisResult(BaseModel):
    """Structured output from the ticket analysis tool_use call."""

    summary: str
    missing_info: list[MissingInfoItem] = Field(default_factory=list)
    odoo_notes: list[SourcedItem] = Field(default_factory=list)
    standard_coverage: StandardCoverage = Field(default_factory=StandardCoverage)
    existing_customizations: ExistingCustomizations = Field(
        default_factory=ExistingCustomizations
    )
    estimates: list[StoryEstimate] = Field(default_factory=list)

    @field_validator("missing_info", "odoo_notes", "estimates", mode="before")
    @classmethod
    def _parse_json_string_list(cls, v: object) -> object:
        return _unwrap_json_list(v)

    @field_validator("summary", mode="before")
    @classmethod
    def _reject_degenerate_output(cls, v: object) -> object:
        return _reject_tool_call_syntax(v)


class Attachment(BaseModel):
    """A file forwarded by Odoo ({filename, content_base64}). Accepted types are
    .docx / .pdf / .txt / .md — the filename extension is the authoritative gate
    (see reva.attachment_text). Shared by the ticket-analysis attachment and the
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
    attachment: Attachment | None = None  # optional .docx/.pdf/.txt/.md, folded into the prompt
    # Optional repo URL from the record's Odoo project, stamped at create time.
    # Used for dashboard repo grouping AND by the worker to ground the analysis
    # in the repo's own custom-addon docs (reva/repo_docs.py, spec 2026-07-14);
    # default None keeps every worker path untouched.
    github_url: str | None = None


# --- Support answer types -----------------------------------------------------

SupportRequestKind = Literal["question", "change_request", "bug_report", "mixed", "other"]
SupportAnswerStatus = Literal["answered", "partially_answered", "cannot_answer"]
SupportLanguage = Literal["de", "en"]


class ChatterEntry(BaseModel):
    """One Odoo chatter message on a support-request ticket."""

    id: int
    posted_at: datetime
    author: str
    author_kind: Literal["customer", "internal", "system"]
    visibility: Literal["public", "internal"]
    body: str


class SupportJobParams(BaseModel):
    """Inputs handed to the support-answer RQ job."""

    turn_id: int
    thread_id: int
    odoo_instance_id: int
    ticket_id: int
    model_name: str  # e.g. "helpdesk.ticket" or "project.task"
    field_name: str
    subject: str
    question: str
    # Optional repo URL from the record's Odoo project; nullable → default persona
    # and no code-grounded path (see support_runner).
    github_url: str | None = None
    # Nullable, consultant-authored, additive — never overrides the persona knobs.
    persona_context: str | None = None
    chatter: list[ChatterEntry]
    attachment: Attachment | None = None  # optional .docx/.pdf/.txt/.md, folded into the prompt


class SupportSource(BaseModel):
    """One citation backing a support answer draft."""

    kind: Literal["core_doc", "repo_doc", "repo_code"]
    ref: str
    title: str = ""

    @field_validator("ref", mode="before")
    @classmethod
    def _truncate_ref(cls, v: object) -> object:
        if isinstance(v, str) and len(v) > 300:
            return v[:297] + "..."
        return v

    @field_validator("title", mode="before")
    @classmethod
    def _truncate_title(cls, v: object) -> object:
        if isinstance(v, str) and len(v) > 200:
            return v[:197] + "..."
        return v


class SupportHandoff(BaseModel):
    """Signal pointing a consultant at the existing ticket-analysis / create-issues
    actions when a support request also carries a change request or bug report."""

    suggest_analysis: bool = False
    suggest_issues: bool = False
    rationale: str = ""

    @field_validator("rationale", mode="before")
    @classmethod
    def _truncate_rationale(cls, v: object) -> object:
        if v is None:
            # The tool schema offers null so the model can say "nothing here"
            # instead of being forced to invent an empty string.
            return ""
        if isinstance(v, str) and len(v) > 1000:
            return v[:997] + "..."
        return v


class SupportAnswerResult(BaseModel):
    """Structured output from the submit_support_answer tool_use call.

    Field population per answer_status:
      answered            — answer set, cannot_answer_reason empty.
      partially_answered  — answer set, open_questions non-empty.
      cannot_answer       — answer empty, cannot_answer_reason required. The
        product's "no caveated draft" rule: say what's missing, don't guess.
    """

    request_kind: SupportRequestKind
    answer_status: SupportAnswerStatus
    answer: str = ""
    cannot_answer_reason: str | None = None
    open_questions: list[str] = Field(default_factory=list)
    sources: list[SupportSource] = Field(default_factory=list)
    handoff: SupportHandoff = Field(default_factory=SupportHandoff)
    language: SupportLanguage
    # Qualitative, matching every other Odoo-facing REVA output
    # (MissingInfoItem, CoverageFeature, StoryEstimate). It was briefly a float:
    # ge/le made the API reject the tool outright ("For 'number' type,
    # properties maximum, minimum are not supported"), and dropping them made
    # the model answer "medium" against a `type: number` field — even with
    # strict:true, additionalProperties:false and every property required.
    # Strict does not reliably coerce numeric types, the prompt saying 0.0-1.0
    # did not help, and nothing consumes this value, so the enum the model
    # already wants to emit is the right shape.
    confidence: Literal["high", "medium", "low"] = "medium"

    @field_validator("open_questions", "sources", mode="before")
    @classmethod
    def _parse_json_string_list(cls, v: object) -> object:
        return _unwrap_json_list(v)

    # Bound long free-text fields so one oversized field can't blow the Odoo
    # HTML write (write_field) — same defensive intent as Finding.body/suggestion.
    @field_validator("answer", mode="before")
    @classmethod
    def _truncate_answer(cls, v: object) -> object:
        if v is None:
            # cannot_answer draws a null here — see _make_nullable in
            # support_tool.py for why the schema offers one.
            return ""
        # Checked before the truncation below, so the marker can't be cut off
        # the end of an oversized draft and pass.
        v = _reject_tool_call_syntax(v)
        if isinstance(v, str) and len(v) > 20000:
            return v[:19997] + "..."
        return v

    @field_validator("cannot_answer_reason", mode="before")
    @classmethod
    def _truncate_cannot_answer_reason(cls, v: object) -> object:
        if isinstance(v, str) and len(v) > 2000:
            return v[:1997] + "..."
        return v

    @field_validator("open_questions", mode="before")
    @classmethod
    def _truncate_open_questions(cls, v: object) -> object:
        if isinstance(v, list):
            return [
                item[:497] + "..." if isinstance(item, str) and len(item) > 500 else item
                for item in v
            ]
        return v

    @model_validator(mode="after")
    def _cannot_answer_contract(self) -> "SupportAnswerResult":
        # Schema-level half of the "no caveated draft" rule — the prompt alone
        # is not trusted to enforce it.
        if self.answer_status == "cannot_answer":
            if not (self.cannot_answer_reason or "").strip():
                raise ValueError(
                    "cannot_answer_reason is required when answer_status is 'cannot_answer'"
                )
            if self.answer.strip():
                raise ValueError("answer must be empty when answer_status is 'cannot_answer'")
        return self


# --- Ticket issue creation types -----------------------------------------------


# Work-item type codes: title prefix + GitHub label on every REVA-created issue.
ISSUE_TYPE_CODES = ("BUG", "FEAT", "CR", "CONF", "DEV", "MIG", "SUP", "DOC")
IssueTypeCode = Literal["BUG", "FEAT", "CR", "CONF", "DEV", "MIG", "SUP", "DOC"]


class TicketIssueItem(BaseModel):
    """One GitHub issue planned from an Odoo ticket."""

    title: str
    body: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    # Defaults to DEV so plans persisted before the type rollout still
    # validate; the runner overrides it when the request fixes a type.
    type: IssueTypeCode = "DEV"
    # Low-end development estimate in hours (implementation + developer testing,
    # mid-level AI-assisted dev). None on plans persisted before the estimate
    # rollout; the tool schema requires it for fresh plans.
    estimate_hours: float | None = None
    # 1-based positions (in this plan's issues array) of the issues this one
    # builds on. The runner re-orders the plan dependency-first and renders the
    # "Builds on (n/total)." body line itself — the planner hand-writing
    # numbering guessed the total wrong (ticket 6324: "(1/3)" in a 4-issue
    # plan). Empty on plans persisted before the rollout.
    builds_on: list[int] = Field(default_factory=list)

    @field_validator("title", mode="before")
    @classmethod
    def _truncate_title(cls, v: object) -> object:
        # Bound well under GitHub's 256-char issue-title limit.
        if isinstance(v, str) and len(v) > 200:
            return v[:197] + "..."
        return v

    @field_validator("acceptance_criteria", "builds_on", mode="before")
    @classmethod
    def _parse_json_string_list(cls, v: object) -> object:
        return _unwrap_json_list(v)


class TicketIssuePlan(BaseModel):
    """Structured output from the submit_ticket_issues tool_use call."""

    # 1–2 sentence plain-English summary of the whole ticket, for the parent
    # epic body. Always English regardless of the ticket's language. Empty on
    # plans persisted before the summary rollout.
    summary: str = ""
    issues: list[TicketIssueItem] = Field(min_length=1, max_length=10)

    @field_validator("issues", mode="before")
    @classmethod
    def _parse_json_string_list(cls, v: object) -> object:
        return _unwrap_json_list(v)


MemoryAction = Literal["dont_flag", "raise_bar", "keep_flagging"]


class ReviewMemoryItem(BaseModel):
    """One distilled learned-guidance item (Tier 3 feature B)."""

    guidance: str
    categories: list[Category] = Field(default_factory=list)
    action: MemoryAction
    evidence_count: int = 0

    @field_validator("categories", mode="before")
    @classmethod
    def _parse_json_string_list(cls, v: object) -> object:
        return _unwrap_json_list(v)


class ReviewMemoryPlan(BaseModel):
    """Structured output from the submit_review_memory tool_use call. No hard
    length cap here — code-side guardrails drop and cap items after validation."""

    items: list[ReviewMemoryItem] = Field(default_factory=list)

    @field_validator("items", mode="before")
    @classmethod
    def _parse_json_string_list(cls, v: object) -> object:
        return _unwrap_json_list(v)


class TicketIssueJobParams(BaseModel):
    """Inputs handed to the create-issues RQ job: the Contract 1 payload from
    the Odoo addon (github-issues handoff) plus the ticket_issue_runs row id,
    which doubles as the request_id Odoo correlates callbacks with."""

    run_id: int
    odoo_instance_id: int
    ticket_id: int
    model_name: str  # e.g. "helpdesk.ticket" or "project.task"
    github_url: str
    name: str
    description: str
    analysis_html: str  # "" when the record has no completed analysis
    description_docx: Attachment | None = None  # tasks only; .docx/.pdf/.txt
    priority: str  # Odoo priority key "0".."3"
    ticket_url: str
    # Fixed work-item type for every issue of this request (Odoo wizard), or
    # None to let the planner pick per issue (analysis flow).
    issue_type: str | None = None
    # Optional GitHub login assigned to every created issue and parent epic.
    github_username: str | None = None
    # Optional Projects v2 board every created issue (and the epic) is added
    # to, and the planned date set on it. Absent → no Projects interaction.
    github_project_url: str | None = None
    plan_date: date | None = None


# --- Timesheet wording review types -----------------------------------------


TIMESHEET_CHUNK_SIZE = 100

TimesheetUserRole = Literal["developer", "consultant", "sales"]
TimesheetLineStatus = Literal["ok", "rewritten", "needs_human"]


class TimesheetLine(BaseModel):
    """One Odoo time-booking line submitted for wording review."""

    line_id: int
    task_name: str
    project_name: str
    user_name: str
    user_role: TimesheetUserRole
    description: str = Field(max_length=4000)


class TimesheetLineResult(BaseModel):
    """Claude's verdict for one line."""

    line_id: int
    status: TimesheetLineStatus
    updated_desc: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _conditional_fields(self) -> "TimesheetLineResult":
        if self.status == "rewritten" and not (self.updated_desc or "").strip():
            raise ValueError("updated_desc is required when status is 'rewritten'")
        if self.status == "needs_human" and not (self.reason or "").strip():
            raise ValueError("reason is required when status is 'needs_human'")
        return self


class TimesheetChunkResult(BaseModel):
    """Validated input of the submit_timesheet_review tool call."""

    results: list[TimesheetLineResult]


class TimesheetJobParams(BaseModel):
    """Inputs handed to the timesheet review RQ job."""

    run_id: int
    odoo_instance_id: int
    request_id: str
    flagged_words: list[str] = Field(default_factory=list)
    lines: list[TimesheetLine]


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
