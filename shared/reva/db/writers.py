"""Read/write helpers used by tasks.run_review and the future api.

The `Database` class exposes:
  - `get_owner_name` / `get_pr_basic`  : satisfy `reviewer.RepoLookup`
  - `record_review_*`                  : persist outcomes of Reviewer.execute
  - `attach_github_ids`                : link Check Run / PR Review IDs after posting
  - `upsert_repository`                : webhook handler entry
  - `upsert_pull_request`              : webhook handler entry
  - `upsert_pending_review`            : debounce mechanism (one row per PR)
  - `record_github_event`              : raw delivery storage

Methods that mutate state run inside their own session/transaction. Read
methods open a short-lived session per call. Callers that need a longer
transaction can use `Database.session()` directly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from reva.db.engine import Database
from reva.db.models import (
    GithubEvent,
    PendingReview,
    PullRequest,
    Repository,
    ReviewFinding,
    ReviewRun,
)
from reva.types import Finding, JobParams, ReviewResult


# --- RepoLookup methods (Reviewer Protocol) ----------------------------------


def get_owner_name(db: Database, repository_id: int) -> tuple[str, str]:
    with db.session() as s:
        row = s.execute(
            select(Repository.owner, Repository.name).where(Repository.id == repository_id)
        ).first()
    if not row:
        raise LookupError(f"repository_id={repository_id} not found")
    return row[0], row[1]


def get_pr_basic(db: Database, pull_request_id: int) -> dict:
    with db.session() as s:
        row = s.execute(
            select(
                PullRequest.pr_number,
                PullRequest.title,
                PullRequest.base_branch,
                PullRequest.head_branch,
            ).where(PullRequest.id == pull_request_id)
        ).first()
    if not row:
        raise LookupError(f"pull_request_id={pull_request_id} not found")
    return {
        "pr_number": row[0],
        "title": row[1],
        "body": "",  # body is fetched from GitHub at review time
        "base_branch": row[2],
        "head_branch": row[3],
    }


# --- review_runs writers -----------------------------------------------------


def record_review_started(db: Database, params: JobParams) -> int:
    """Insert/UPDATE a review_runs row in `running` status.

    Idempotent on the unique constraint `(repo, pr, head_sha, review_mode)`:
    the second call with the same params updates the existing row.
    """
    with db.session() as s:
        run = _upsert_review_run(s, params, status="running")
        run.started_at = datetime.now(timezone.utc)
        s.flush()
        return run.id


def record_review_completed(db: Database, params: JobParams, result: ReviewResult) -> int:
    """Persist a completed ReviewResult plus findings. Idempotent."""
    with db.session() as s:
        run = _upsert_review_run(s, params, status="completed")
        run.model = result.model
        run.prompt_version = result.prompt_version
        run.started_at = result.started_at
        run.completed_at = result.completed_at
        run.duration_ms = result.duration_ms
        run.input_tokens = result.input_tokens
        run.output_tokens = result.output_tokens
        run.cache_read_tokens = result.cache_read_tokens
        run.cache_creation_tokens = result.cache_creation_tokens
        run.estimated_cost_usd = result.estimated_cost_usd
        run.risk_level = result.risk_level
        run.summary = result.summary
        run.finding_count = len(result.findings)
        run.decline_reason = None
        run.error_message = None
        run.error_class = None
        s.flush()
        _replace_findings(s, run.id, result.findings)
        return run.id


def record_review_declined(db: Database, params: JobParams, reason: str) -> int:
    with db.session() as s:
        run = _upsert_review_run(s, params, status="declined")
        run.decline_reason = reason
        run.summary = reason
        run.completed_at = datetime.now(timezone.utc)
        run.finding_count = 0
        s.flush()
        _replace_findings(s, run.id, [])
        return run.id


def record_review_stale(db: Database, params: JobParams) -> int:
    with db.session() as s:
        run = _upsert_review_run(s, params, status="stale")
        run.completed_at = datetime.now(timezone.utc)
        run.summary = "Head SHA changed before review started."
        s.flush()
        return run.id


def record_review_failed(
    db: Database, params: JobParams, error_class: str, message: str
) -> int:
    with db.session() as s:
        run = _upsert_review_run(s, params, status="failed")
        run.error_class = error_class
        run.error_message = message
        run.completed_at = datetime.now(timezone.utc)
        s.flush()
        return run.id


def is_already_posted(db: Database, params: JobParams) -> bool:
    """Return True iff a review_runs row for these params has check_run_id set.

    Used for RQ-retry idempotency: skip the post step if a prior attempt
    already created the Check Run.
    """
    with db.session() as s:
        row = s.execute(
            select(ReviewRun.check_run_id).where(
                (ReviewRun.repository_id == params.repository_id)
                & (ReviewRun.pull_request_id == params.pull_request_id)
                & (ReviewRun.head_sha == params.head_sha)
                & (ReviewRun.review_mode == params.review_mode)
            )
        ).first()
    return bool(row and row[0] is not None)


def attach_github_ids(
    db: Database,
    review_run_id: int,
    check_run_id: int | None = None,
    review_id: int | None = None,
) -> None:
    """Set the GitHub Check Run and/or PR Review IDs after posting."""
    with db.session() as s:
        run = s.get(ReviewRun, review_run_id)
        if run is None:
            raise LookupError(f"review_run_id={review_run_id} not found")
        if check_run_id is not None:
            run.check_run_id = check_run_id
        if review_id is not None:
            run.review_id = review_id


# --- repositories / pull_requests / pending_reviews / events -----------------


def upsert_repository(
    db: Database,
    github_repository_id: int,
    owner: str,
    name: str,
    default_branch: str,
    installation_id: int,
) -> int:
    with db.session() as s:
        repo = s.execute(
            select(Repository).where(Repository.github_repository_id == github_repository_id)
        ).scalar_one_or_none()
        if repo is None:
            repo = Repository(
                github_repository_id=github_repository_id,
                owner=owner,
                name=name,
                full_name=f"{owner}/{name}",
                default_branch=default_branch,
                installation_id=installation_id,
            )
            s.add(repo)
            s.flush()
        else:
            repo.owner = owner
            repo.name = name
            repo.full_name = f"{owner}/{name}"
            repo.default_branch = default_branch
            repo.installation_id = installation_id
            repo.updated_at = datetime.now(timezone.utc)
        return repo.id


def upsert_pull_request(
    db: Database,
    repository_id: int,
    github_pr_id: int,
    pr_number: int,
    title: str,
    author_login: str | None,
    base_branch: str,
    head_branch: str,
    head_sha: str,
    state: str,
    draft: bool,
) -> int:
    with db.session() as s:
        pr = s.execute(
            select(PullRequest).where(
                (PullRequest.repository_id == repository_id)
                & (PullRequest.pr_number == pr_number)
            )
        ).scalar_one_or_none()
        if pr is None:
            pr = PullRequest(
                repository_id=repository_id,
                github_pr_id=github_pr_id,
                pr_number=pr_number,
                title=title,
                author_login=author_login,
                base_branch=base_branch,
                head_branch=head_branch,
                head_sha=head_sha,
                state=state,
                draft=draft,
            )
            s.add(pr)
            s.flush()
        else:
            pr.title = title
            pr.author_login = author_login
            pr.base_branch = base_branch
            pr.head_branch = head_branch
            pr.head_sha = head_sha
            pr.state = state
            pr.draft = draft
            pr.updated_at = datetime.now(timezone.utc)
        return pr.id


def upsert_pending_review(
    db: Database,
    repository_id: int,
    pull_request_id: int,
    pr_number: int,
    head_sha: str,
    installation_id: int,
    trigger_event: str,
    review_mode: str,
    scheduled_at: datetime,
) -> int:
    """Upsert the single pending row for a PR.

    The unique constraint on (repository_id, pr_number) is the debounce
    mechanism: every new push overwrites scheduled_at and head_sha,
    keeping at most one queued review per PR.
    """
    with db.session() as s:
        existing = s.execute(
            select(PendingReview).where(
                (PendingReview.repository_id == repository_id)
                & (PendingReview.pr_number == pr_number)
            )
        ).scalar_one_or_none()
        if existing is None:
            row = PendingReview(
                repository_id=repository_id,
                pull_request_id=pull_request_id,
                pr_number=pr_number,
                head_sha=head_sha,
                installation_id=installation_id,
                trigger_event=trigger_event,
                review_mode=review_mode,
                scheduled_at=scheduled_at,
                consumed=False,
            )
            s.add(row)
            s.flush()
            return row.id
        existing.head_sha = head_sha
        existing.installation_id = installation_id
        existing.trigger_event = trigger_event
        existing.review_mode = review_mode
        existing.scheduled_at = scheduled_at
        existing.consumed = False
        existing.updated_at = datetime.now(timezone.utc)
        return existing.id


def record_github_event(
    db: Database,
    delivery_id: str,
    event_type: str,
    action: str | None,
    repository_full_name: str | None,
    sender_login: str | None,
    payload: dict,
) -> int | None:
    """Idempotent insert keyed on delivery_id. Returns the row id, or None
    if a row with this delivery_id already existed."""
    with db.session() as s:
        existing = s.execute(
            select(GithubEvent.id).where(GithubEvent.delivery_id == delivery_id)
        ).first()
        if existing:
            return None
        ev = GithubEvent(
            delivery_id=delivery_id,
            event_type=event_type,
            action=action,
            repository_full_name=repository_full_name,
            sender_login=sender_login,
            payload=payload,
        )
        s.add(ev)
        s.flush()
        return ev.id


# --- internals --------------------------------------------------------------


def _upsert_review_run(s, params: JobParams, status: str) -> ReviewRun:
    """Fetch-or-create a review_runs row idempotent on the unique constraint."""
    run = s.execute(
        select(ReviewRun).where(
            (ReviewRun.repository_id == params.repository_id)
            & (ReviewRun.pull_request_id == params.pull_request_id)
            & (ReviewRun.head_sha == params.head_sha)
            & (ReviewRun.review_mode == params.review_mode)
        )
    ).scalar_one_or_none()
    if run is None:
        run = ReviewRun(
            repository_id=params.repository_id,
            pull_request_id=params.pull_request_id,
            head_sha=params.head_sha,
            review_mode=params.review_mode,
            trigger_event=params.trigger_event,
            status=status,
        )
        s.add(run)
        s.flush()
    else:
        run.status = status
        run.trigger_event = params.trigger_event
    return run


def _replace_findings(s, review_run_id: int, findings: list[Finding]) -> None:
    """Replace any existing findings for a run. Idempotent retries don't dupe."""
    s.query(ReviewFinding).filter(ReviewFinding.review_run_id == review_run_id).delete(
        synchronize_session=False
    )
    for f in findings:
        s.add(
            ReviewFinding(
                review_run_id=review_run_id,
                severity=f.severity,
                category=f.category,
                file_path=f.file,
                line_start=f.line_start,
                line_end=f.line_end,
                title=f.title,
                body=f.body,
                suggestion=f.suggestion,
                confidence=f.confidence,
                is_odoo_specific=f.is_odoo_specific,
            )
        )
    s.flush()
