"""Read queries for review_runs, review_findings, and related tables."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import case, func, select

from reva.db import writers
from reva.db.engine import Database
from reva.db.models import PendingReview, PullRequest, Repository, ReviewFeedback, ReviewFinding, ReviewRun

# Severity sort order for findings (critical first).
_SEVERITY_ORDER = case(
    (ReviewFinding.severity == "critical", 0),
    (ReviewFinding.severity == "major", 1),
    (ReviewFinding.severity == "minor", 2),
    else_=3,
)


def list_reviews(
    db: Database,
    *,
    repo: str | None = None,
    statuses: list[str] | None = None,
    author: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    with db.session() as s:
        base = (
            select(
                ReviewRun,
                Repository.full_name.label("repo_full_name"),
                PullRequest.pr_number,
                PullRequest.title.label("pr_title"),
                PullRequest.author_login,
            )
            .join(Repository, ReviewRun.repository_id == Repository.id)
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
        )
        if repo:
            base = base.where(Repository.full_name == repo)
        if statuses:
            base = base.where(ReviewRun.status.in_(statuses))
        if author:
            base = base.where(PullRequest.author_login == author)

        total = s.execute(select(func.count()).select_from(base.subquery())).scalar_one()

        rows = s.execute(
            base.order_by(ReviewRun.created_at.desc()).limit(limit).offset(offset)
        ).all()

        items = [
            {
                "id": rr.id,
                "repo_full_name": repo_full_name,
                "pr_number": pr_number,
                "pr_title": pr_title,
                "author_login": author_login,
                "head_sha": rr.head_sha,
                "status": rr.status,
                "review_mode": rr.review_mode,
                "model": rr.model,
                "risk_level": rr.risk_level,
                "finding_count": rr.finding_count,
                "duration_ms": rr.duration_ms,
                "estimated_cost_usd": (
                    float(rr.estimated_cost_usd) if rr.estimated_cost_usd is not None else None
                ),
                "created_at": rr.created_at,
            }
            for rr, repo_full_name, pr_number, pr_title, author_login in rows
        ]
    return items, total


def get_review_detail(db: Database, review_run_id: int) -> dict | None:
    with db.session() as s:
        row = s.execute(
            select(
                ReviewRun,
                Repository.full_name.label("repo_full_name"),
                PullRequest.pr_number,
                PullRequest.title.label("pr_title"),
                PullRequest.author_login,
            )
            .join(Repository, ReviewRun.repository_id == Repository.id)
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .where(ReviewRun.id == review_run_id)
        ).one_or_none()

        if row is None:
            return None

        rr, repo_full_name, pr_number, pr_title, author_login = row

        # Findings with aggregated feedback counts.
        finding_rows = s.execute(
            select(
                ReviewFinding,
                func.count(
                    case((ReviewFeedback.is_positive == True, ReviewFeedback.id))  # noqa: E712
                ).label("thumbs_up"),
                func.count(
                    case((ReviewFeedback.is_positive == False, ReviewFeedback.id))  # noqa: E712
                ).label("thumbs_down"),
            )
            .outerjoin(ReviewFeedback, ReviewFeedback.review_finding_id == ReviewFinding.id)
            .where(ReviewFinding.review_run_id == review_run_id)
            .group_by(ReviewFinding.id)
            .order_by(_SEVERITY_ORDER, ReviewFinding.id)
        ).all()

        findings = [
            {
                "id": f.id,
                "severity": f.severity,
                "category": f.category,
                "title": f.title,
                "confidence": float(f.confidence) if f.confidence is not None else None,
                "file_path": f.file_path,
                "line_start": f.line_start,
                "body": f.body,
                "suggestion": f.suggestion,
                "is_odoo_specific": f.is_odoo_specific,
                "thumbs_up": thumbs_up,
                "thumbs_down": thumbs_down,
            }
            for f, thumbs_up, thumbs_down in finding_rows
        ]

    return {
        "id": rr.id,
        "repo_full_name": repo_full_name,
        "pr_number": pr_number,
        "pr_title": pr_title,
        "author_login": author_login,
        "head_sha": rr.head_sha,
        "status": rr.status,
        "review_mode": rr.review_mode,
        "model": rr.model,
        "risk_level": rr.risk_level,
        "finding_count": rr.finding_count,
        "duration_ms": rr.duration_ms,
        "estimated_cost_usd": (
            float(rr.estimated_cost_usd) if rr.estimated_cost_usd is not None else None
        ),
        "created_at": rr.created_at,
        "summary": rr.summary,
        "decline_reason": rr.decline_reason,
        "error_message": rr.error_message,
        "error_class": rr.error_class,
        "input_tokens": rr.input_tokens,
        "output_tokens": rr.output_tokens,
        "findings": findings,
    }


def list_findings(
    db: Database,
    *,
    severities: list[str] | None = None,
    category: str | None = None,
    repo: str | None = None,
    limit: int = 100,
) -> tuple[list[dict], int]:
    with db.session() as s:
        base = select(ReviewFinding)
        if severities:
            base = base.where(ReviewFinding.severity.in_(severities))
        if category:
            base = base.where(ReviewFinding.category == category)
        if repo:
            base = (
                base
                .join(ReviewRun, ReviewFinding.review_run_id == ReviewRun.id)
                .join(Repository, ReviewRun.repository_id == Repository.id)
                .where(Repository.full_name == repo)
            )

        total = s.execute(select(func.count()).select_from(base.subquery())).scalar_one()
        findings = s.execute(
            base.order_by(_SEVERITY_ORDER, ReviewFinding.id.desc()).limit(limit)
        ).scalars().all()

        items = [
            {
                "id": f.id,
                "severity": f.severity,
                "category": f.category,
                "title": f.title,
                "confidence": float(f.confidence) if f.confidence is not None else None,
                "file_path": f.file_path,
                "line_start": f.line_start,
            }
            for f in findings
        ]
    return items, total


def list_failures(db: Database, *, limit: int = 20) -> tuple[list[dict], int]:
    with db.session() as s:
        base = (
            select(
                ReviewRun,
                Repository.full_name.label("repo_full_name"),
                PullRequest.pr_number,
                PullRequest.title.label("pr_title"),
                PullRequest.author_login,
            )
            .join(Repository, ReviewRun.repository_id == Repository.id)
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .where(ReviewRun.status.in_(["failed", "stale"]))
        )

        total = s.execute(select(func.count()).select_from(base.subquery())).scalar_one()
        rows = s.execute(base.order_by(ReviewRun.created_at.desc()).limit(limit)).all()

        items = [
            {
                "id": rr.id,
                "repo_full_name": repo_full_name,
                "pr_number": pr_number,
                "pr_title": pr_title or "",
                "author_login": author_login,
                "head_sha": rr.head_sha,
                "status": rr.status,
                "review_mode": rr.review_mode,
                "model": rr.model,
                "risk_level": rr.risk_level,
                "finding_count": rr.finding_count,
                "duration_ms": rr.duration_ms,
                "estimated_cost_usd": (
                    float(rr.estimated_cost_usd) if rr.estimated_cost_usd is not None else None
                ),
                "created_at": rr.created_at,
                "summary": rr.summary,
                "decline_reason": rr.decline_reason,
                "error_message": rr.error_message,
                "error_class": rr.error_class,
                "input_tokens": rr.input_tokens,
                "output_tokens": rr.output_tokens,
                "findings": [],
            }
            for rr, repo_full_name, pr_number, pr_title, author_login in rows
        ]
    return items, total


def list_pending(db: Database) -> tuple[list[dict], int]:
    with db.session() as s:
        base = (
            select(
                PendingReview,
                Repository.full_name.label("repo_full_name"),
                PullRequest.title.label("pr_title"),
            )
            .join(Repository, PendingReview.repository_id == Repository.id)
            .join(PullRequest, PendingReview.pull_request_id == PullRequest.id)
            .where(PendingReview.consumed == False)  # noqa: E712
        )
        total = s.execute(select(func.count()).select_from(base.subquery())).scalar_one()
        rows = s.execute(base.order_by(PendingReview.scheduled_at.asc())).all()
        items = [
            {
                "id": pr.id,
                "repo_full_name": repo_full_name,
                "pr_number": pr.pr_number,
                "pr_title": pr_title or "",
                "head_sha": pr.head_sha,
                "scheduled_at": pr.scheduled_at,
                "trigger_event": pr.trigger_event,
                "review_mode": pr.review_mode,
            }
            for pr, repo_full_name, pr_title in rows
        ]
    return items, total


def requeue_review(db: Database, review_run_id: int) -> bool:
    """Reset a failed/stale review to be re-run immediately.

    Returns False if the review_run doesn't exist or isn't in a terminal state.
    """
    with db.session() as s:
        row = s.execute(
            select(
                ReviewRun,
                Repository.installation_id,
                PullRequest.pr_number,
            )
            .join(Repository, ReviewRun.repository_id == Repository.id)
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .where(ReviewRun.id == review_run_id)
            .where(ReviewRun.status.in_(["failed", "stale"]))
        ).one_or_none()

    if row is None:
        return False

    rr, installation_id, pr_number = row
    writers.upsert_pending_review(
        db,
        repository_id=rr.repository_id,
        pull_request_id=rr.pull_request_id,
        pr_number=pr_number,
        head_sha=rr.head_sha,
        installation_id=installation_id,
        trigger_event="manual_requeue",
        review_mode=rr.review_mode,
        scheduled_at=datetime.now(timezone.utc),
    )
    return True
