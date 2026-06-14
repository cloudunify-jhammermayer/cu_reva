"""SQLAlchemy 2.0 typed declarative models for REVA.

Mirrors the SQL DDL in db/migrations/*.sql. SQLite is supported for tests
via type translation (BIGSERIAL -> INTEGER PRIMARY KEY AUTOINCREMENT,
JSONB -> JSON, TIMESTAMPTZ -> TIMESTAMP).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
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


# --------------------------------------------------------- ticket_analyses


class TicketAnalysis(Base):
    __tablename__ = "ticket_analyses"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(Text)
    ticket_id: Mapped[int] = mapped_column(Integer, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    field_name: Mapped[str] = mapped_column(Text, nullable=False)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    result_html: Mapped[str | None] = mapped_column(Text)
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
    )


# ------------------------------------------------------- ticket_issue_runs


class TicketIssueRun(Base):
    __tablename__ = "ticket_issue_runs"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(Text)
    ticket_id: Mapped[int] = mapped_column(Integer, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    github_url: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    analysis_html: Mapped[str] = mapped_column(Text, nullable=False)
    # What this run plans from: "docx:<hash>" or "text:<hash>" (migration 014).
    # A 25-byte digest, not the document — the consultant DOCX itself is never
    # stored server-side; it rides the RQ job params at first-plan time only.
    planning_basis: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(Text, nullable=False)
    ticket_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    # The issue plan and its creation progress:
    # [{"title", "body", "acceptance_criteria", "number", "url"}, ...]
    # number/url stay null until the issue exists on GitHub.
    issues: Mapped[Any | None] = mapped_column(JSON)
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
        # One in-flight run per Odoo record (closes the dedup check-then-insert race).
        Index(
            "idx_ticket_issue_runs_pending",
            "ticket_id",
            "model_name",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index("idx_ticket_issue_runs_status", "status"),
        Index("idx_ticket_issue_runs_ticket_id", "ticket_id"),
        Index("idx_ticket_issue_runs_created_at", "created_at"),
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

    __table_args__ = (Index("idx_weekly_reports_enqueued", "enqueued_at"),)


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
