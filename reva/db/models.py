"""SQLAlchemy 2.0 typed declarative models for REVA.

Mirrors the SQL DDL in db/migrations/*.sql. SQLite is supported for tests
via type translation (BIGSERIAL -> INTEGER PRIMARY KEY AUTOINCREMENT,
JSONB -> JSON, TIMESTAMPTZ -> TIMESTAMP).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# BIGSERIAL on Postgres → INTEGER PRIMARY KEY AUTOINCREMENT on SQLite.
# SQLite's autoincrement only fires when the PK column is exactly INTEGER.
_PK = BigInteger().with_variant(Integer, "sqlite")


# ---------------------------------------------------------------- repositories


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    github_repository_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str | None] = mapped_column(Text, default="main")
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config_cache: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    config_cached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("idx_repos_full_name", "full_name"),)


# --------------------------------------------------------------- pull_requests


class PullRequest(Base):
    __tablename__ = "pull_requests"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    repository_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repositories.id"), nullable=False
    )
    github_pr_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    author_login: Mapped[str | None] = mapped_column(Text)
    base_branch: Mapped[str] = mapped_column(Text, nullable=False)
    head_branch: Mapped[str] = mapped_column(Text, nullable=False)
    head_sha: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    draft: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at_github: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at_github: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("repository_id", "pr_number", name="uq_pull_requests_repo_number"),
        Index("idx_prs_repo_number", "repository_id", "pr_number"),
        Index("idx_prs_author", "author_login"),
    )


# ------------------------------------------------------------- pending_reviews


class PendingReview(Base):
    __tablename__ = "pending_reviews"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    repository_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repositories.id"), nullable=False
    )
    pull_request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("pull_requests.id"), nullable=False
    )
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(Text, nullable=False)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    trigger_event: Mapped[str] = mapped_column(Text, nullable=False)
    review_mode: Mapped[str] = mapped_column(Text, nullable=False, default="diff")
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("repository_id", "pr_number", name="uq_pending_reviews_repo_number"),
        # Partial index on Postgres; plain index on SQLite (postgresql_where ignored there).
        Index(
            "idx_pending_reviews_scheduled",
            "scheduled_at",
            postgresql_where=text("consumed = FALSE"),
        ),
    )


# ---------------------------------------------------------------- review_runs


class ReviewRun(Base):
    __tablename__ = "review_runs"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    repository_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repositories.id"), nullable=False
    )
    pull_request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("pull_requests.id"), nullable=False
    )
    head_sha: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    trigger_event: Mapped[str] = mapped_column(Text, nullable=False)
    review_mode: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    risk_level: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    finding_count: Mapped[int] = mapped_column(Integer, default=0)
    decline_reason: Mapped[str | None] = mapped_column(Text)
    check_run_id: Mapped[int | None] = mapped_column(BigInteger)
    review_id: Mapped[int | None] = mapped_column(BigInteger)
    error_message: Mapped[str | None] = mapped_column(Text)
    error_class: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str | None] = mapped_column(Text)
    claimed_by_job_id: Mapped[str | None] = mapped_column(Text)  # CONC-1 atomic claim
    # Learned-memory version injected into this review's prompt (migration 024),
    # or NULL when no memory was active. Attribution for dismiss-rate trends.
    learned_memory_version: Mapped[int | None] = mapped_column(Integer)
    # Triage pre-pass escalation ("full"/"deep"), NULL = none/off.
    triage_escalation: Mapped[str | None] = mapped_column(Text)
    # Per-linked-issue conformance verdicts (migration 036): JSON list of
    # {issue_number, verdict, note}, NULL when the run produced none.
    intent_check: Mapped[Any | None] = mapped_column(JSON)
    # Set when an explicit re-review clears the row's posted state; scopes crash
    # recovery to the current attempt (H3). NULL until first re-review.
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Force-push-aware delta + cross-branch reuse (migration 042).
    diff_hash: Mapped[str | None] = mapped_column(Text)
    delta_base_sha: Mapped[str | None] = mapped_column(Text)
    carried_from_run_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "pull_request_id",
            "head_sha",
            "review_mode",
            name="uq_review_runs_idempotency",
        ),
        Index("idx_review_runs_repo_created", "repository_id", text("created_at DESC")),
        Index("idx_review_runs_status", "status"),
        Index("idx_review_runs_pr", "pull_request_id", text("created_at DESC")),
        # Serves the unfiltered global /reviews feed's ORDER BY created_at DESC
        # (migration 021) — the composite indexes above don't.
        Index("idx_review_runs_created", text("created_at DESC")),
    )


# ------------------------------------------------------------ review_findings


class ReviewFinding(Base):
    __tablename__ = "review_findings"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    review_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False
    )
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str | None] = mapped_column(Text)
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Numeric(3, 2))
    is_odoo_specific: Mapped[bool] = mapped_column(Boolean, default=False)
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger)
    posted_to_github: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Per-finding outcome ledger (migration 015): 'open' -> 'resolved_by_fix'
    # (verifier confirmed a fix on a later push) or 'still_open_at_merge' (PR
    # merged with the finding never observed fixed).
    outcome: Mapped[str] = mapped_column(
        Text, nullable=False, default="open", server_default=text("'open'")
    )
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_findings_run", "review_run_id"),
        Index("idx_findings_severity", "severity"),
        Index("idx_findings_category", "category"),
        Index("idx_findings_file", "file_path"),
        # Partial index (migration 004) for matching reply webhooks to findings.
        Index(
            "idx_findings_github_comment_id",
            "github_comment_id",
            postgresql_where=text("github_comment_id IS NOT NULL"),
            sqlite_where=text("github_comment_id IS NOT NULL"),
        ),
        # Partial index (migration 015) for the outcome-ledger dashboard.
        Index(
            "idx_findings_outcome",
            "outcome",
            postgresql_where=text("outcome <> 'open'"),
            sqlite_where=text("outcome <> 'open'"),
        ),
    )


# ------------------------------------------------------------- github_events


class GithubEvent(Base):
    __tablename__ = "github_events"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    delivery_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str | None] = mapped_column(Text)
    repository_full_name: Mapped[str | None] = mapped_column(Text)
    sender_login: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[Any] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_events_received", text("received_at DESC")),
        Index("idx_events_repo", "repository_full_name", text("received_at DESC")),
    )


# ----------------------------------------------------------------- review_jobs


class ReviewJob(Base):
    __tablename__ = "review_jobs"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    rq_job_id: Mapped[str | None] = mapped_column(Text, unique=True)
    repository_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repositories.id"), nullable=False
    )
    pull_request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("pull_requests.id"), nullable=False
    )
    head_sha: Mapped[str] = mapped_column(Text, nullable=False)
    review_mode: Mapped[str] = mapped_column(Text, nullable=False, default="diff")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("idx_jobs_status", "status"),)


# ----------------------------------------------------------- review_feedback


class ReviewFeedback(Base):
    __tablename__ = "review_feedback"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    review_finding_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_findings.id", ondelete="CASCADE"), nullable=False
    )
    review_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False
    )
    github_comment_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reactor_login: Mapped[str] = mapped_column(Text, nullable=False)
    reaction: Mapped[str] = mapped_column(Text, nullable=False)
    is_positive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "review_finding_id", "reactor_login", "reaction",
            name="uq_review_feedback_unique",
        ),
        Index("idx_feedback_finding", "review_finding_id"),
        Index("idx_feedback_run", "review_run_id"),
        Index("idx_feedback_positive", "is_positive"),
    )


# ----------------------------------------------------------- muted_categories


class MutedCategory(Base):
    __tablename__ = "muted_categories"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    repository_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(Text, nullable=False)
    muted_by: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("repository_id", "category", name="uq_muted_category"),
        # Partial, matching migration 016 — the lookup only wants active mutes.
        Index(
            "idx_muted_categories_repo", "repository_id",
            postgresql_where=text("active"), sqlite_where=text("active"),
        ),
    )


# ------------------------------------------------------ repo_review_memory


class RepoReviewMemory(Base):
    """Per-repo learned review guidance (Tier 3 / feature B; migration 024).

    Append-only versions distilled from dismissed-finding history; exactly one
    active row per repo (record_repo_memory deactivates the prior version in the
    same transaction). content "" = distillation produced nothing to inject."""

    __tablename__ = "repo_review_memory"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    repository_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    items: Mapped[Any | None] = mapped_column(JSON)
    source_stats: Mapped[Any | None] = mapped_column(JSON)
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("repository_id", "version", name="uq_repo_memory_version"),
        Index(
            "idx_repo_review_memory_active", "repository_id",
            postgresql_where=text("active"), sqlite_where=text("active"),
        ),
    )


# --------------------------------------------------------- ticket_analyses


class TicketAnalysis(Base):
    __tablename__ = "ticket_analyses"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(Text)
    ticket_id: Mapped[int] = mapped_column(Integer, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    field_name: Mapped[str] = mapped_column(Text, nullable=False)
    odoo_instance_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("odoo_instances.id")
    )
    # Optional repo URL from the record's Odoo project (migration 038), stamped
    # at create time for dashboard repo grouping. NULL for legacy/analysis-only.
    github_url: Mapped[str | None] = mapped_column(Text)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    result_html: Mapped[str | None] = mapped_column(Text)
    result_structured: Mapped[Any | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Delivery visibility (migration 033): the Odoo write_field callback happens
    # after the row is 'completed'; callback_sent_at is NULL until it lands.
    callback_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    callback_error: Mapped[str | None] = mapped_column(Text)
    # How many customer-repo doc sections were injected into this analysis
    # (migration 039). NULL = retrieval never attempted (no github_url / resume
    # path / legacy row); 0 = attempted, nothing injected; N = sections injected.
    repo_docs_sections_used: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        # Partial UNIQUE index (migration 006): job_id is unique only when set.
        Index(
            "idx_ticket_analyses_job_id",
            "job_id",
            unique=True,
            postgresql_where=text("job_id IS NOT NULL"),
            sqlite_where=text("job_id IS NOT NULL"),
        ),
        Index("idx_ticket_analyses_status", "status"),
        Index("idx_ticket_analyses_ticket_id", "ticket_id"),
        # List endpoint orders by created_at DESC — migration 025.
        Index("idx_ticket_analyses_created_at", "created_at"),
        # One pending analysis per (instance, ticket, model, field) — migration
        # 020. Backs the submit dedup against a concurrent-POST race (M10).
        Index(
            "idx_ticket_analyses_pending",
            "odoo_instance_id",
            "ticket_id",
            "model_name",
            "field_name",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )


# ------------------------------------------------------- ticket_issue_runs


class TicketIssueRun(Base):
    __tablename__ = "ticket_issue_runs"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(Text)
    ticket_id: Mapped[int] = mapped_column(Integer, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    odoo_instance_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("odoo_instances.id")
    )
    github_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Lowercased "owner/repo" derived from github_url at creation (migration 022).
    # Lets update_ticket_issue_state equality-match on an index instead of a
    # leading-wildcard ILIKE full scan (M15). NULL only for pre-022 rows the
    # backfill couldn't parse.
    repo_full_name: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    analysis_html: Mapped[str] = mapped_column(Text, nullable=False)
    # What this run plans from: "docx:<hash>" or "text:<hash>" (migration 014).
    # A 25-byte digest, not the document — the consultant DOCX itself is never
    # stored server-side; it rides the RQ job params at first-plan time only.
    planning_basis: Mapped[str | None] = mapped_column(Text)
    # Fixed work-item type for this request ("CR", "BUG", …; migration 023),
    # or NULL when the planner picks per issue.
    issue_type: Mapped[str | None] = mapped_column(Text)
    # Optional GitHub assignee for child issues and the parent epic.
    github_username: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(Text, nullable=False)
    ticket_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional Projects v2 board + planned date (migration 034); NULL → no
    # Projects interaction for this run.
    github_project_url: Mapped[str | None] = mapped_column(Text)
    plan_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    # The issue plan and its creation progress:
    # [{"title", "body", "acceptance_criteria", "number", "url"}, ...]
    # number/url stay null until the issue exists on GitHub.
    issues: Mapped[Any | None] = mapped_column(JSON)
    # Plain-English ticket summary for the parent epic body (migration 035);
    # NULL on pre-rollout runs.
    plan_summary: Mapped[str | None] = mapped_column(Text)
    # The parent ("epic") issue grouping this ticket's sub-issues, or NULL for
    # legacy and single-issue runs: {number, id, url, title, state}. Excluded
    # from every Odoo payload by design (it lives only on GitHub).
    parent_issue: Mapped[Any | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Partial UNIQUE index (migration 012): job_id is unique only when set.
        Index(
            "idx_ticket_issue_runs_job_id",
            "job_id",
            unique=True,
            postgresql_where=text("job_id IS NOT NULL"),
            sqlite_where=text("job_id IS NOT NULL"),
        ),
        # One in-flight run per Odoo INSTANCE per record (migration 018).
        Index(
            "idx_ticket_issue_runs_pending",
            "odoo_instance_id",
            "ticket_id",
            "model_name",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index("idx_ticket_issue_runs_status", "status"),
        Index("idx_ticket_issue_runs_ticket_id", "ticket_id"),
        Index("idx_ticket_issue_runs_created_at", "created_at"),
        # Equality lookup by repo for issue state-sync webhooks (M15).
        Index("idx_ticket_issue_runs_repo_full_name", "repo_full_name"),
    )


class TicketIssueReassignment(Base):
    """Mirrors db/migrations/047_ticket_issue_reassignments.sql — an operator
    correction of which Odoo record owns a REVA-created issue (spec
    2026-08-20).

    Absence is the normal case: ownership is otherwise implicit in
    `ticket_issue_runs.issues`, and those rows are never rewritten by a move, so
    deleting one of these rows undoes it. `odoo_instance_id` is NOT NULL even
    though the runs table allows NULL for legacy rows — the endpoint that writes
    this is instance-key gated, so every row it can receive has one.
    """

    __tablename__ = "ticket_issue_reassignments"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    odoo_instance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("odoo_instances.id"), nullable=False
    )
    # Lowercased "owner/repo", matching TicketIssueRun.repo_full_name.
    repo_full_name: Mapped[str] = mapped_column(Text, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "uq_ticket_issue_reassignments",
            "odoo_instance_id",
            "repo_full_name",
            "number",
            unique=True,
        ),
        Index(
            "idx_ticket_issue_reassignments_record",
            "odoo_instance_id",
            "ticket_id",
            "model_name",
        ),
    )


# ------------------------------------------------------------- change_notes


class ChangeNote(Base):
    __tablename__ = "change_notes"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    repo_full_name: Mapped[str] = mapped_column(Text, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    odoo_instance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("odoo_instances.id"), nullable=False
    )
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    note_html: Mapped[str | None] = mapped_column(Text)
    # 'claude' (drafted from the diff) or 'release-log' (the ticket's entry in
    # docs/releases/<name>.md, re-read at delivery; note_html stays "").
    source: Mapped[str] = mapped_column(Text, nullable=False, default="claude")
    # PR title/url captured at generation time so the batched change-summary
    # (assembled later from the DB) renders each PR ref without a GitHub call.
    pr_title: Mapped[str | None] = mapped_column(Text)
    pr_url: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Stamped when the row was shipped in a change-summary batch; NULL until then.
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "idx_change_notes_dedup",
            "repo_full_name",
            "pr_number",
            "ticket_id",
            unique=True,
        ),
        Index("idx_change_notes_created_at", "created_at"),
    )


# ------------------------------------------------------------ ticket_actuals


class TicketActual(Base):
    """Mirrors db/migrations/040_ticket_actuals.sql — per-ticket timesheet
    actuals pushed by Odoo when a ticket is marked done (estimate-calibration
    loop C1). Estimates stay on the Projects board; one row per (instance,
    ticket), latest push wins."""

    __tablename__ = "ticket_actuals"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    odoo_instance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("odoo_instances.id"), nullable=False
    )
    ticket_id: Mapped[int] = mapped_column(Integer, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    actual_hours: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    timesheet_line_count: Mapped[int | None] = mapped_column(Integer)
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "idx_ticket_actuals_instance_ticket",
            "odoo_instance_id",
            "ticket_id",
            "model_name",
            unique=True,
        ),
    )


# ------------------------------------------------------------- value_reports


class ValueReport(Base):
    __tablename__ = "value_reports"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    stats: Mapped[Any | None] = mapped_column(JSON)
    chat_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_value_reports_period", "period_start", "period_end", unique=True),
    )


# ------------------------------------------------------- timesheet reviews


class TimesheetReviewRun(Base):
    __tablename__ = "timesheet_review_runs"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(Text)
    odoo_instance_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("odoo_instances.id")
    )
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    total_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ok_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rewritten_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    needs_human_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    callback_payload: Mapped[Any | None] = mapped_column(JSON)
    callback_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "idx_timesheet_runs_pending",
            "odoo_instance_id",
            "request_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index("idx_timesheet_runs_created", text("created_at DESC")),
        Index("idx_timesheet_runs_status", "status"),
    )


class TimesheetReviewLine(Base):
    __tablename__ = "timesheet_review_lines"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("timesheet_review_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    line_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_timesheet_lines_run_line", "run_id", "line_id", unique=True),
    )


# ----------------------------------------------------------------- release_notes


class ReleaseNote(Base):
    """One Odoo release-log lookup (migration 048). `id` is the note_id Odoo
    echoes on the callback. No content: the repo page is the source of truth,
    the row records where it was found and how the exchange ended."""

    __tablename__ = "release_notes"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(Text)
    odoo_instance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("odoo_instances.id"), nullable=False
    )
    release_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    release_name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    source_repo_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("repositories.id")
    )
    source_path: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    callback_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_release_notes_created", text("created_at DESC")),
        Index("idx_release_notes_instance_release", "odoo_instance_id", "release_id"),
        Index(
            "idx_release_notes_pending",
            "odoo_instance_id",
            "release_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )


# --------------------------------------------------------------- odoo_instances


class OdooInstance(Base):
    """An Odoo instance that sends work to REVA. Mirrors db/migrations/018.

    `key_hash` is the SHA-256 of the REVA-minted inbound key (plaintext shown
    once at create/rotate). `callback_api_key_enc` is the Fernet-encrypted
    outbound Bearer REVA sends to this Odoo's callback endpoints.
    """

    __tablename__ = "odoo_instances"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    callback_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    callback_api_key_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Per-instance quotas (migration 026): NULL = unlimited.
    daily_budget_usd: Mapped[float | None] = mapped_column(Numeric(12, 2))
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer)
    # Which Odoo version this instance's tickets are analysed against.
    odoo_version: Mapped[str | None] = mapped_column(Text)
    # Default instance for the no-linked-issue PR fallback (migration 041):
    # extracted ticket ids REVA has never seen resolve here. At most one row
    # set — partial unique index below; setting it is a manual deploy step.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Partial UNIQUE index (migration 041): at most one default instance.
        Index(
            "uq_odoo_instances_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default"),
        ),
    )


# --------------------------------------------------------------- audit_runs


class AuditRun(Base):
    __tablename__ = "audit_runs"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    repository_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repositories.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="started")
    requested_by: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    finding_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_audit_runs_repository_id", "repository_id"),
        Index("idx_audit_runs_status", "status"),
    )


class AuditFinding(Base):
    __tablename__ = "audit_findings"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    audit_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("audit_runs.id", ondelete="CASCADE"), nullable=False
    )
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str | None] = mapped_column(Text)
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Numeric(3, 2))
    is_odoo_specific: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set when a major/critical finding is opened as a GitHub issue.
    github_issue_number: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_audit_findings_run", "audit_run_id"),
        Index("idx_audit_findings_severity", "severity"),
    )


# ------------------------------------------------------------- claude_spend


class ClaudeSpend(Base):
    """Mirrors db/migrations/009_claude_spend.sql. One row per paid Claude call
    (review/audit/reply); the single accounting source for the rolling budget
    cap (sum_estimated_cost_since)."""

    __tablename__ = "claude_spend"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("idx_claude_spend_created", "created_at"),)


# ------------------------------------------------------------------ ops_events


class OpsEvent(Base):
    """A caught-and-degraded component error (mirrors db/migrations/027)."""

    __tablename__ = "ops_events"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    component: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[Any | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_ops_events_created_at", "created_at"),
        Index("idx_ops_events_component", "component"),
    )


# ------------------------------------------------------ core-knowledge registry


class OdooCoreModule(Base):
    """One core/enterprise addon module (mirrors db/migrations/028)."""

    __tablename__ = "odoo_core_modules"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    odoo_version: Mapped[str] = mapped_column(Text, nullable=False)
    module: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    depends: Mapped[Any | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("idx_core_modules_version", "odoo_version", "module"),)


class OdooCoreModel(Base):
    """One model definition/inheritance in core (mirrors db/migrations/028)."""

    __tablename__ = "odoo_core_models"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    odoo_version: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    module: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("idx_core_models_version_model", "odoo_version", "model"),)


class OdooCoreField(Base):
    """One field definition in core (mirrors db/migrations/028)."""

    __tablename__ = "odoo_core_fields"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    odoo_version: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    ftype: Mapped[str | None] = mapped_column(Text)
    module: Mapped[str] = mapped_column(Text, nullable=False)
    string: Mapped[str | None] = mapped_column(Text)
    compute: Mapped[str | None] = mapped_column(Text)
    related: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("idx_core_fields_version_model", "odoo_version", "model"),)


class OdooDocsSection(Base):
    """One heading-delimited section of the official docs."""

    __tablename__ = "odoo_docs_sections"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    odoo_version: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    anchor: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("idx_docs_sections_version", "odoo_version"),)


class CoreKnowledgeVersion(Base):
    """Load bookkeeping: one row per loaded version."""

    __tablename__ = "core_knowledge_versions"

    odoo_version: Mapped[str] = mapped_column(Text, primary_key=True)
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    modules: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    models: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fields: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sections: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RepoDocSection(Base):
    """One heading-delimited section of a customer repo's custom-addon docs
    (migration 039). Lazily synced from the repo's default branch at ticket-
    analysis time; keyed by lowercased owner/repo. The Postgres GIN FTS index
    lives in the migration only (SQLite tests use the ilike fallback)."""

    __tablename__ = "repo_doc_sections"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    repo_full_name: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    anchor: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("idx_repo_doc_sections_repo", "repo_full_name"),)


class RepoDocsSync(Base):
    """Sync bookkeeping: one row per repo recording which tree SHA is indexed
    (migration 039; scope_version migration 045). Drives the lazy staleness
    check in reva/repo_docs.py::sync_repo_docs."""

    __tablename__ = "repo_docs_sync"

    repo_full_name: Mapped[str] = mapped_column(Text, primary_key=True)
    tree_sha: Mapped[str] = mapped_column(Text, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sections: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scope_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


# ------------------------------------------------- support_threads / _turns


class SupportThread(Base):
    """One REVA<->consultant Q&A conversation about an Odoo record (migration
    044). Keyed including field_name so two delivery targets on one record can
    coexist, matching idx_ticket_analyses_pending."""

    __tablename__ = "support_threads"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    odoo_instance_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("odoo_instances.id")
    )
    ticket_id: Mapped[int] = mapped_column(Integer, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    field_name: Mapped[str] = mapped_column(Text, nullable=False)
    github_url: Mapped[str | None] = mapped_column(Text)
    # The resolved persona as actually applied, so a thread's tone stays
    # auditable after someone edits the persona rows.
    persona_snapshot: Mapped[Any | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_turn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "odoo_instance_id", "ticket_id", "model_name", "field_name",
            name="uq_support_threads_record",
        ),
    )


class SupportTurn(Base):
    """One question/answer round inside a thread (migration 044).

    `odoo_instance_id` is denormalised from the thread so
    writers.sum_instance_cost_since can sum support spend alongside the other
    run tables without a join — the per-instance budget gate reads one shape.
    """

    __tablename__ = "support_turns"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("support_threads.id"), nullable=False
    )
    odoo_instance_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("odoo_instances.id")
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[str | None] = mapped_column(Text)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer_html: Mapped[str | None] = mapped_column(Text)
    result_structured: Mapped[Any | None] = mapped_column(JSON)
    request_kind: Mapped[str | None] = mapped_column(Text)
    answer_status: Mapped[str | None] = mapped_column(Text)
    grounding_level: Mapped[str | None] = mapped_column(Text)
    # Images submitted with the question (migration 046). The bytes are not
    # stored, so this is what tells an operator a requeued turn answered blind.
    image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    callback_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    callback_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_support_turns_seq"),
        # One pending turn per thread — backs the submit dedup against a
        # concurrent-POST race (migration 044).
        Index(
            "uq_support_turns_pending",
            "thread_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index("idx_support_turns_created_at", "created_at"),
    )


# -------------------------------------------------------------------- personas


class Persona(Base):
    """Tone configuration for support-answer drafts (migration 043).

    Resolved per field as default < repo < the additive persona_context Odoo
    sends per request, so a repo row may leave any knob NULL to inherit the
    default. `repo_full_name` is a lowercased "owner/repo" TEXT key rather than
    an FK — a support request can name a repo REVA has no webhook history for
    (same reasoning as RepoDocSection).
    """

    __tablename__ = "personas"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    repo_full_name: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    formality: Mapped[str | None] = mapped_column(Text)
    technical_depth: Mapped[str | None] = mapped_column(Text)
    length: Mapped[str | None] = mapped_column(Text)
    salutation: Mapped[str | None] = mapped_column(Text)
    sign_off: Mapped[str | None] = mapped_column(Text)
    style_notes: Mapped[str | None] = mapped_column(Text)
    # Separate from style_notes so it renders as a hard constraint, not tone.
    content_policy: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Partial UNIQUE indexes (migration 043): one persona per repo, and at
        # most one default row for project-less requests to fall back to.
        Index(
            "uq_personas_repo",
            "repo_full_name",
            unique=True,
            postgresql_where=text("scope = 'repo'"),
            sqlite_where=text("scope = 'repo'"),
        ),
        Index(
            "uq_personas_default",
            "scope",
            unique=True,
            postgresql_where=text("scope = 'default'"),
            sqlite_where=text("scope = 'default'"),
        ),
    )


# ------------------------------------------------------------- weekly_reports


class WeeklyReport(Base):
    """Mirrors db/migrations/005_weekly_reports.sql. The scheduler reads/writes
    this via raw SQL; the model exists so the table is part of the schema
    source of truth and is created in `create_all()`-based test DBs."""

    __tablename__ = "weekly_reports"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)

    __table_args__ = (Index("idx_weekly_reports_enqueued", text("enqueued_at DESC")),)


# ----------------------------------------------------------- prompt_versions


class PromptVersion(Base):
    __tablename__ = "prompt_versions"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    system_prompt_hash: Mapped[str] = mapped_column(Text, nullable=False)
    review_prompt_hash: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --------------------------------------------------------------- admin_audit


class AdminAudit(Base):
    """Mirrors db/migrations/008_admin_audit.sql — who/what/when for privileged
    /api/v1 admin actions (requeue, manual review, trigger audit, weekly report)."""

    __tablename__ = "admin_audit"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[Any | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("idx_admin_audit_created", text("created_at DESC")),)
