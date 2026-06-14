"""Write helpers used by tasks.run_review and the future api.

  - `record_review_*`   : persist outcomes of Reviewer.execute
  - `attach_github_ids` : link Check Run / PR Review IDs after posting
  - `upsert_repository` : webhook handler entry
  - `upsert_pull_request` : webhook handler entry
  - `upsert_pending_review` : debounce mechanism (one row per PR)
  - `record_github_event` : raw delivery storage

Read helpers (get_owner_name, get_pr_basic) live in reva.db.repo_lookup.

All mutations run inside their own session/transaction. Callers that need a
longer transaction can use `Database.session()` directly.
"""

from __future__ import annotations

import functools
import hashlib
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError

from reva.cost import estimate_cost
from reva.db.engine import Database
from reva.db.models import (
    AdminAudit,
    AuditFinding,
    ClaudeSpend,
    GithubEvent,
    PendingReview,
    PromptVersion,
    PullRequest,
    Repository,
    ReviewFinding,
    ReviewRun,
    TicketAnalysis,
    TicketIssueRun,
)
from reva.types import (
    ClaudeResponse,
    Finding,
    JobParams,
    ReviewResult,
    TicketIssueJobParams,
    TicketJobParams,
)

logger = structlog.get_logger()


def _retry_on_conflict(fn):
    """Re-run a SELECT-then-INSERT upsert once if a concurrent writer wins the race.

    These upserts have a TOCTOU window: two transactions can both see no row
    and both INSERT, with the loser raising IntegrityError on the unique
    constraint. session() rolls the loser back; the retry's SELECT now finds
    the row and takes the UPDATE branch. Upserts are pure w.r.t. their args, so
    re-running is safe.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except IntegrityError as exc:
            # Only a unique-constraint race is fixable by re-running (the retry's
            # SELECT now finds the row). Re-raise other integrity errors (FK,
            # NOT NULL, CHECK) immediately — retrying won't help and would mask
            # the original, more informative error (CORR-17).
            if not _is_unique_violation(exc):
                raise
            return fn(*args, **kwargs)

    return wrapper


def _is_unique_violation(exc: IntegrityError) -> bool:
    # Postgres (psycopg2): SQLSTATE 23505. SQLite: message "UNIQUE constraint failed".
    if getattr(getattr(exc, "orig", None), "pgcode", None) == "23505":
        return True
    return "unique constraint" in str(exc).lower()


# --- review_runs writers -----------------------------------------------------


@_retry_on_conflict
def claim_review_run(db: Database, params: JobParams, job_id: str | None) -> tuple[int, bool]:
    """Atomically claim the (repo, pr, head_sha, review_mode) review for `job_id`.

    Returns (run_id, claimed). `claimed=False` means a **different** worker job
    already holds this exact review in `running` — the caller MUST NOT run the
    paid review (CONC-1: prevents two jobs both paying for the same SHA when
    multiple workers race a push-debounce + /review, a torn poller tick, etc.).

    Retry-safe: a retry of the *same* job_id re-claims (so RQ retries complete),
    and a non-running row (completed/failed/declined) is re-claimed — that's the
    explicit re-review path. The row is locked FOR UPDATE so concurrent claimers
    serialize on Postgres; on SQLite the single writer serializes anyway.
    """
    now = datetime.now(timezone.utc)
    with db.session() as s:
        existing = s.execute(
            select(ReviewRun)
            .where(
                (ReviewRun.repository_id == params.repository_id)
                & (ReviewRun.pull_request_id == params.pull_request_id)
                & (ReviewRun.head_sha == params.head_sha)
                & (ReviewRun.review_mode == params.review_mode)
            )
            .with_for_update()
        ).scalar_one_or_none()

        if existing is None:
            run = ReviewRun(
                repository_id=params.repository_id,
                pull_request_id=params.pull_request_id,
                head_sha=params.head_sha,
                review_mode=params.review_mode,
                trigger_event=params.trigger_event,
                status="running",
                started_at=now,
                claimed_by_job_id=job_id,
            )
            s.add(run)
            s.flush()
            return run.id, True

        # A different live job already owns this in-flight review → don't run it.
        if existing.status == "running" and existing.claimed_by_job_id not in (None, job_id):
            return existing.id, False

        existing.status = "running"
        existing.started_at = now
        existing.claimed_by_job_id = job_id
        existing.trigger_event = params.trigger_event
        s.flush()
        return existing.id, True


def record_review_started(db: Database, params: JobParams) -> int:
    """Start (or re-claim) a review_runs row in `running` status; returns its id.

    Thin convenience over claim_review_run with no job identity — always claims.
    Used for seeding/tests; the worker uses claim_review_run to honour the
    duplicate-in-flight guard (CONC-1).
    """
    run_id, _ = claim_review_run(db, params, job_id=None)
    return run_id


def reset_review_run_post_state(db: Database, review_run_id: int) -> None:
    """Clear a run's posted GitHub IDs + prior outcome so an explicit re-review
    (a /review comment or a manual requeue) posts a fresh review instead of
    reusing the stale Check Run / PR Review IDs from the earlier attempt."""
    with db.session() as s:
        run = s.get(ReviewRun, review_run_id)
        if run is None:
            return
        run.check_run_id = None
        run.review_id = None
        run.completed_at = None
        run.decline_reason = None
        run.error_class = None
        run.error_message = None


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
        # Record spend atomically with the run so the rolling cap counts it.
        _insert_spend(s, "review", result.estimated_cost_usd)
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


def record_review_skipped_trivial(db: Database, params: JobParams, summary: str) -> int:
    """Record a review short-circuited as trivial (no Claude call, no spend)."""
    with db.session() as s:
        run = _upsert_review_run(s, params, status="skipped_trivial")
        run.completed_at = datetime.now(timezone.utc)
        run.summary = summary
        run.finding_count = 0
        s.flush()
        _replace_findings(s, run.id, [])
        return run.id


def reap_stale_running_reviews(db: Database, older_than_seconds: int) -> int:
    """Fail review_runs stuck in `running` longer than older_than_seconds.

    A worker SIGKILLed mid-review (forced container stop, OOM, crash) leaves its
    row in `running` forever — no terminal write ever lands. Called periodically
    by the scheduler with a threshold well above the job timeout, so only truly
    dead runs are swept (never a live, long-running review).

    Returns the number of rows reaped.

    CONC-8: the stale rows are locked FOR UPDATE SKIP LOCKED so concurrent
    scheduler replicas don't double-reap the same row (and can't clobber a row a
    peer is mid-reaping). On SQLite the clause no-ops (single writer).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
    with db.session() as s:
        stale = s.execute(
            select(ReviewRun).where(
                ReviewRun.status == "running",
                ReviewRun.started_at < cutoff,
            ).with_for_update(skip_locked=True)
        ).scalars().all()
        for run in stale:
            run.status = "failed"
            run.error_class = "stale"
            run.error_message = (
                f"Reaped: stuck in 'running' >{older_than_seconds}s "
                "(worker likely died mid-review)."
            )
            run.completed_at = datetime.now(timezone.utc)
        if stale:
            logger.warning("review_runs_reaped", count=len(stale))
        return len(stale)


def record_review_failed(
    db: Database, params: JobParams, error_class: str, message: str
) -> int:
    with db.session() as s:
        run = _upsert_review_run(s, params, status="failed")
        run.error_class = error_class
        run.error_message = message
        run.completed_at = datetime.now(timezone.utc)
        s.flush()
        logger.warning(
            "review_run_failed",
            run_id=run.id,
            error_class=error_class,
            repository_id=params.repository_id,
            pull_request_id=params.pull_request_id,
        )
        return run.id


def is_already_posted(db: Database, params: JobParams) -> bool:
    """Return True iff a *successfully posted* run for these params exists.

    Used for RQ-retry idempotency: skip the whole job if a prior attempt
    already created the Check Run. A `failed` run is excluded — its
    check_run_id is the failure notice, not a real review, so a requeue/retry
    must be allowed to produce a genuine review.
    """
    with db.session() as s:
        row = s.execute(
            select(ReviewRun.check_run_id, ReviewRun.status).where(
                (ReviewRun.repository_id == params.repository_id)
                & (ReviewRun.pull_request_id == params.pull_request_id)
                & (ReviewRun.head_sha == params.head_sha)
                & (ReviewRun.review_mode == params.review_mode)
            )
        ).first()
    return bool(row and row[0] is not None and row[1] != "failed")


# Arbitrary fixed key for the budget advisory lock ("REVB" as an int).
_BUDGET_ADVISORY_LOCK_KEY = 0x52455642


def _insert_spend(s, kind: str, cost_usd: float | None) -> None:
    """Append a spend-ledger row within an existing session (atomic with caller)."""
    s.add(ClaudeSpend(kind=kind, cost_usd=cost_usd or 0.0))


def record_claude_spend(db: Database, kind: str, cost_usd: float | None) -> None:
    """Record one paid Claude call in the unified spend ledger (own transaction).

    Used by the audit and comment-reply paths; the review path records its spend
    atomically inside record_review_completed.
    """
    with db.session() as s:
        _insert_spend(s, kind, cost_usd)


def sum_estimated_cost_since(db: Database, since: datetime, *, serialize: bool = False) -> float:
    """Total estimated USD cost of ALL Claude calls (reviews, audits, replies)
    recorded in the claude_spend ledger at/after `since`.

    Used by the worker's rolling spend cap — the ledger is the single accounting
    source so the cap sees every kind of Claude spend, not just reviews.

    With serialize=True the read is taken under a transaction-level advisory
    lock on Postgres, so concurrent workers evaluate the cap one at a time
    rather than racing on interleaved reads. (No-op on SQLite.) Residual
    overshoot is still bounded by the number of concurrent workers — at most one
    in-flight call each — which is accepted; the cap is a rolling guardrail.
    """
    from sqlalchemy import func as _func

    with db.session() as s:
        if serialize and s.get_bind().dialect.name == "postgresql":
            s.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": _BUDGET_ADVISORY_LOCK_KEY},
            )
        total = s.execute(
            select(_func.coalesce(_func.sum(ClaudeSpend.cost_usd), 0.0)).where(
                ClaudeSpend.created_at >= since
            )
        ).scalar_one()
    return float(total or 0.0)


def get_posted_github_ids(db: Database, review_run_id: int) -> tuple[int | None, int | None]:
    """Return (check_run_id, review_id) currently stored for a run.

    Lets the post path skip a GitHub call whose ID is already persisted, so a
    retry after a partial post (e.g. PR review created but Check Run failed)
    does not create a duplicate PR review.
    """
    with db.session() as s:
        row = s.execute(
            select(ReviewRun.check_run_id, ReviewRun.review_id).where(
                ReviewRun.id == review_run_id
            )
        ).first()
    if row is None:
        return None, None
    return row[0], row[1]


def get_review_run_created_at(db: Database, review_run_id: int) -> datetime | None:
    """created_at of a run — used to scope review recovery to this run's era so a
    stale prior review (run-id reuse / DB reset) isn't recovered (PR-9 fix)."""
    with db.session() as s:
        return s.execute(
            select(ReviewRun.created_at).where(ReviewRun.id == review_run_id)
        ).scalar_one_or_none()


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


@_retry_on_conflict
def register_prompt_version(
    db: Database,
    version: str,
    system_prompt_hash: str,
    review_prompt_hash: str,
    description: str | None = None,
) -> str:
    """Record the prompt content hashes for `version` in prompt_versions.

    Returns one of:
      "created"   — first time this version string is seen; row inserted.
      "unchanged" — version exists and both hashes match the stored baseline.
      "drift"     — version exists but a hash differs: a prompt file changed
                    without bumping the version. The stored row is left untouched
                    so the first-seen hashes remain the baseline for the version.

    NOTE: system_prompt_hash stores sha256(review_guidance.md) and
    review_prompt_hash stores sha256(odoo19.md + skills/*.md); the column names
    are inherited from an earlier Messages-API design and do not reflect the
    current CLI pipeline (see PromptBuilder.compute_prompt_hashes).
    """
    with db.session() as s:
        row = s.execute(
            select(PromptVersion).where(PromptVersion.version == version)
        ).scalar_one_or_none()
        if row is None:
            s.add(
                PromptVersion(
                    version=version,
                    system_prompt_hash=system_prompt_hash,
                    review_prompt_hash=review_prompt_hash,
                    description=description,
                )
            )
            return "created"
        if (
            row.system_prompt_hash == system_prompt_hash
            and row.review_prompt_hash == review_prompt_hash
        ):
            return "unchanged"
        return "drift"


@_retry_on_conflict
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
            logger.info("repository_registered", full_name=f"{owner}/{name}", installation_id=installation_id)
        else:
            repo.owner = owner
            repo.name = name
            repo.full_name = f"{owner}/{name}"
            repo.default_branch = default_branch
            repo.installation_id = installation_id
            repo.updated_at = datetime.now(timezone.utc)
        return repo.id


@_retry_on_conflict
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


# Review-mode strength, low → high. A queued review is never downgraded by an
# auto event (CORR-7); see upsert_pending_review.
_MODE_PRECEDENCE = {"diff": 0, "diff-all": 1, "full": 2, "deep": 3}


@_retry_on_conflict
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
        existing.scheduled_at = scheduled_at
        existing.consumed = False
        existing.updated_at = datetime.now(timezone.utc)
        # CORR-7: don't let an auto event (a push/synchronize, default `diff`)
        # silently downgrade a stronger queued review. An explicit comment
        # command is authoritative (the user just asked); otherwise keep the
        # higher-intent mode. The new head_sha/schedule above still apply, so the
        # stronger review just runs against the latest commit.
        if trigger_event == "comment" or _MODE_PRECEDENCE.get(
            review_mode, 0
        ) >= _MODE_PRECEDENCE.get(existing.review_mode, 0):
            existing.review_mode = review_mode
            existing.trigger_event = trigger_event
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
    """Record a delivery and return the row id to process, or None to skip.

    Idempotent on delivery_id, but keyed on the `processed` flag rather than
    mere existence: a delivery that was recorded but never marked processed
    (a prior handler crashed mid-way) is handed back so a GitHub redelivery can
    finish it. Only a *fully processed* delivery is skipped as a duplicate.
    Call mark_event_processed() once handling succeeds.
    """
    try:
        with db.session() as s:
            existing = s.execute(
                select(GithubEvent).where(GithubEvent.delivery_id == delivery_id)
            ).scalar_one_or_none()
            if existing is not None:
                if existing.processed:
                    logger.info(
                        "github_event_duplicate",
                        delivery_id=delivery_id,
                        event_type=event_type,
                    )
                    return None
                # Recorded but never finished — let the caller reprocess it.
                return existing.id
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
    except IntegrityError:
        # Concurrent request inserted the same delivery_id between our SELECT and INSERT.
        return None


def record_admin_action(
    db: Database,
    *,
    action: str,
    actor: str | None = None,
    target: str | None = None,
    detail: dict | None = None,
) -> int:
    """Append an audit-log row for a privileged /api/v1 admin action.

    `action` is the verb (e.g. "requeue"), `target` what it acted on, `actor`
    the caller identity (source IP / proxy header), `detail` any extras.
    """
    with db.session() as s:
        row = AdminAudit(action=action, actor=actor, target=target, detail=detail)
        s.add(row)
        s.flush()
        return row.id


def mark_event_processed(db: Database, event_id: int) -> None:
    """Mark a github_events row fully processed so redeliveries are skipped.

    Called only after all downstream work (upserts, enqueue) for the delivery
    has committed — so a crash before this leaves the event reprocessable.
    """
    with db.session() as s:
        ev = s.get(GithubEvent, event_id)
        if ev is not None:
            ev.processed = True
            ev.processed_at = datetime.now(timezone.utc)


def lookup_pull_request(
    db: Database,
    repository_id: int,
    pr_number: int,
) -> dict | None:
    """Return {id, head_sha, installation_id} for a known PR, or None."""
    with db.session() as s:
        row = s.execute(
            select(
                PullRequest.id,
                PullRequest.head_sha,
                Repository.installation_id,
            )
            .join(Repository, PullRequest.repository_id == Repository.id)
            .where(
                PullRequest.repository_id == repository_id,
                PullRequest.pr_number == pr_number,
            )
        ).first()
    if not row:
        return None
    return {"id": row[0], "head_sha": row[1], "installation_id": row[2]}


# --- finding comment IDs -----------------------------------------------------


def get_findings_for_run(db: Database, review_run_id: int) -> list[dict]:
    """Return all findings for a run with their DB id and location info."""
    with db.session() as s:
        rows = s.execute(
            select(
                ReviewFinding.id,
                ReviewFinding.file_path,
                ReviewFinding.line_start,
                ReviewFinding.line_end,
            ).where(ReviewFinding.review_run_id == review_run_id)
        ).all()
    return [
        {"id": r[0], "file_path": r[1], "line_start": r[2], "line_end": r[3]}
        for r in rows
    ]


def insert_audit_findings(db: Database, audit_run_id: int, findings: list[Finding]) -> list[int]:
    """Persist an audit's findings. Returns the new row ids in input order so the
    caller can attach GitHub issue numbers to specific findings."""
    ids: list[int] = []
    with db.session() as s:
        for f in findings:
            row = AuditFinding(
                audit_run_id=audit_run_id,
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
            s.add(row)
            s.flush()
            ids.append(row.id)
        s.commit()
    return ids


def set_audit_finding_issue_number(db: Database, finding_id: int, issue_number: int) -> None:
    """Record the GitHub issue opened for an audit finding."""
    with db.session() as s:
        s.execute(
            update(AuditFinding)
            .where(AuditFinding.id == finding_id)
            .values(github_issue_number=issue_number)
        )
        s.commit()


def get_open_findings_for_pr(db: Database, pull_request_id: int, before_run_id: int | None = None) -> list[dict]:
    """Return findings with a github_comment_id from the most recent completed review.

    Pass before_run_id to exclude the current run and target the prior review.
    """
    with db.session() as s:
        subq = (
            select(ReviewRun.id)
            .where(ReviewRun.pull_request_id == pull_request_id)
            .where(ReviewRun.status == "completed")
        )
        if before_run_id is not None:
            subq = subq.where(ReviewRun.id < before_run_id)
        subq = subq.order_by(ReviewRun.completed_at.desc()).limit(1).scalar_subquery()
        rows = s.execute(
            select(
                ReviewFinding.id,
                ReviewFinding.file_path,
                ReviewFinding.line_start,
                ReviewFinding.title,
                ReviewFinding.body,
                ReviewFinding.severity,
                ReviewFinding.category,
                ReviewFinding.github_comment_id,
            )
            .where(ReviewFinding.review_run_id == subq)
            .where(ReviewFinding.github_comment_id.is_not(None))
        ).all()
    return [
        {
            "id": r[0],
            "file_path": r[1],
            "line_start": r[2],
            "title": r[3],
            "body": r[4],
            "severity": r[5],
            "category": r[6],
            "github_comment_id": r[7],
        }
        for r in rows
    ]


def attach_finding_comment_ids(db: Database, finding_id_to_comment_id: dict[int, int]) -> None:
    """Write github_comment_id + posted_to_github=True for a batch of findings."""
    with db.session() as s:
        for finding_id, comment_id in finding_id_to_comment_id.items():
            finding = s.get(ReviewFinding, finding_id)
            if finding is not None:
                finding.github_comment_id = comment_id
                finding.posted_to_github = True


def set_finding_outcome(db: Database, finding_id: int, outcome: str) -> None:
    """Set a single finding's outcome (e.g. 'resolved_by_fix'). Idempotent UPDATE by id."""
    with db.session() as s:
        s.execute(
            update(ReviewFinding)
            .where(ReviewFinding.id == finding_id)
            .values(outcome=outcome, outcome_at=datetime.now(timezone.utc))
        )


def mark_open_findings_at_merge(db: Database, pull_request_id: int) -> int:
    """Mark every still-open POSTED finding on a merged PR as 'still_open_at_merge'.

    Only findings actually shown to the developer (github_comment_id IS NOT NULL)
    and still 'open' (not already resolved_by_fix) are touched, so resolved_by_fix
    wins and a redelivered merge webhook is a no-op. Returns the rows updated.
    """
    with db.session() as s:
        run_ids = select(ReviewRun.id).where(ReviewRun.pull_request_id == pull_request_id)
        result = s.execute(
            update(ReviewFinding)
            .where(
                ReviewFinding.review_run_id.in_(run_ids),
                ReviewFinding.outcome == "open",
                ReviewFinding.github_comment_id.is_not(None),
            )
            .values(outcome="still_open_at_merge", outcome_at=datetime.now(timezone.utc))
        )
        return result.rowcount


def lookup_finding_by_comment_id(db: Database, github_comment_id: int) -> dict | None:
    """Return finding details for a given github_comment_id, or None."""
    with db.session() as s:
        row = s.execute(
            select(
                ReviewFinding.id,
                ReviewFinding.severity,
                ReviewFinding.title,
                ReviewFinding.body,
                ReviewFinding.file_path,
                ReviewFinding.line_start,
                ReviewFinding.suggestion,
            ).where(ReviewFinding.github_comment_id == github_comment_id)
        ).first()
    if row is None:
        return None
    return {
        "id": row[0],
        "severity": row[1],
        "title": row[2],
        "body": row[3],
        "file_path": row[4],
        "line_start": row[5],
        "suggestion": row[6],
    }


# --- ticket_analyses writers -------------------------------------------------


def record_ticket_analysis_created(db: Database, params: TicketJobParams) -> int:
    """Insert a pending ticket_analyses row and return its id."""
    with db.session() as s:
        row = TicketAnalysis(
            ticket_id=params.ticket_id,
            model_name=params.model_name,
            field_name=params.field_name,
            input_text=params.text,
            status="pending",
        )
        s.add(row)
        s.flush()
        return row.id


def attach_ticket_job_id(db: Database, analysis_id: int, job_id: str) -> None:
    """Store the RQ job ID on the ticket_analyses row after enqueuing."""
    with db.session() as s:
        row = s.get(TicketAnalysis, analysis_id)
        if row is not None:
            row.job_id = job_id


def record_ticket_analysis_completed(
    db: Database,
    analysis_id: int,
    result_html: str,
    response: ClaudeResponse,
) -> None:
    """Mark a ticket analysis as completed and store the result."""
    with db.session() as s:
        row = s.get(TicketAnalysis, analysis_id)
        if row is None:
            return
        row.status = "completed"
        row.result_html = result_html
        row.model = response.model
        row.input_tokens = response.input_tokens
        row.output_tokens = response.output_tokens
        row.cache_read_tokens = response.cache_read_tokens
        row.cache_creation_tokens = response.cache_creation_tokens
        row.estimated_cost_usd = estimate_cost(
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cache_read_tokens=response.cache_read_tokens,
            cache_write_tokens=response.cache_creation_tokens,
        )
        row.completed_at = datetime.now(timezone.utc)


def record_ticket_analysis_failed(
    db: Database,
    analysis_id: int,
    error_message: str,
) -> None:
    """Mark a ticket analysis as failed."""
    with db.session() as s:
        row = s.get(TicketAnalysis, analysis_id)
        if row is None:
            return
        row.status = "failed"
        row.error_message = error_message
        row.completed_at = datetime.now(timezone.utc)


def reset_ticket_analysis(db: Database, analysis_id: int) -> None:
    """Reset a failed ticket analysis to pending so it can be re-enqueued."""
    with db.session() as s:
        row = s.get(TicketAnalysis, analysis_id)
        if row is None:
            return
        row.status = "pending"
        row.error_message = None
        row.completed_at = None
        row.job_id = None


PURGED_TICKET_TEXT = "[purged: exceeded retention period]"


def purge_old_ticket_text(db: Database, older_than_days: int) -> int:
    """Scrub raw customer ticket text older than `older_than_days` (F1/SECU-8).

    Replaces `input_text` with a sentinel (the column is NOT NULL) for rows past
    the retention window, keeping the derived analysis (`result_html`). Raw
    customer-authored text is PII and shouldn't be retained indefinitely
    (data-minimisation / erasure). Idempotent — already-purged rows are skipped.
    Returns the number of rows scrubbed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    with db.session() as s:
        result = s.execute(
            update(TicketAnalysis)
            .where(
                TicketAnalysis.created_at < cutoff,
                TicketAnalysis.input_text != PURGED_TICKET_TEXT,
            )
            .values(input_text=PURGED_TICKET_TEXT)
        )
        return result.rowcount


def get_pending_ticket_analysis(
    db: Database, ticket_id: int, model_name: str, field_name: str
) -> dict | None:
    """Return the most recent pending analysis for this record, or None."""
    with db.session() as s:
        row = s.execute(
            select(TicketAnalysis)
            .where(
                TicketAnalysis.ticket_id == ticket_id,
                TicketAnalysis.model_name == model_name,
                TicketAnalysis.field_name == field_name,
                TicketAnalysis.status == "pending",
            )
            .order_by(TicketAnalysis.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return {"id": row.id, "job_id": row.job_id, "status": row.status}


def get_ticket_analysis(db: Database, analysis_id: int) -> dict | None:
    """Return a ticket_analyses row as a dict, or None."""
    with db.session() as s:
        row = s.get(TicketAnalysis, analysis_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "job_id": row.job_id,
            "ticket_id": row.ticket_id,
            "model_name": row.model_name,
            "field_name": row.field_name,
            "input_text": row.input_text,
            "status": row.status,
            "result_html": row.result_html,
            "error_message": row.error_message,
            "model": row.model,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "estimated_cost_usd": float(row.estimated_cost_usd) if row.estimated_cost_usd else None,
            "created_at": row.created_at,
            "completed_at": row.completed_at,
        }


# --- ticket_issue_runs writers -------------------------------------------------


def compute_planning_basis(params: TicketIssueJobParams) -> str:
    """Content-addressed digest of WHAT a run plans from.

    "docx:<sha1[:16]>" when a consultant document is attached (its content is
    the basis), else "text:<sha1[:16]>" over description + analysis. The prefix
    lets requeue tell a docx run apart without keeping the document; the hash
    lets a re-run detect a revised spec. NOT a security hash — stability across
    a run and its requeues is the only requirement."""
    if params.description_docx is not None:
        key = "docx\x00" + params.description_docx.content_base64
        prefix = "docx:"
    else:
        key = "text\x00" + params.description + "\x00" + params.analysis_html
        prefix = "text:"
    digest = hashlib.sha1(  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
        key.encode(), usedforsecurity=False
    ).hexdigest()[:16]
    return prefix + digest


def record_ticket_issue_run_created(db: Database, params: TicketIssueJobParams) -> int:
    """Insert a pending ticket_issue_runs row and return its id.

    The row id doubles as the request_id Odoo stores and the callback echoes
    (github-issues handoff, Contracts 1+2)."""
    with db.session() as s:
        row = TicketIssueRun(
            ticket_id=params.ticket_id,
            model_name=params.model_name,
            github_url=params.github_url,
            name=params.name,
            description=params.description,
            analysis_html=params.analysis_html,
            planning_basis=compute_planning_basis(params),
            priority=params.priority,
            ticket_url=params.ticket_url,
            status="pending",
        )
        s.add(row)
        s.flush()
        return row.id


def attach_ticket_issue_job_id(db: Database, run_id: int, job_id: str) -> None:
    """Store the RQ job ID on the ticket_issue_runs row after enqueuing."""
    with db.session() as s:
        row = s.get(TicketIssueRun, run_id)
        if row is not None:
            row.job_id = job_id


def get_pending_ticket_issue_run(
    db: Database, ticket_id: int, model_name: str
) -> dict | None:
    """Return the most recent pending run for this record, or None.

    Request dedup: a re-click while a run is in flight gets the SAME
    request_id back, so the in-flight run's callback still matches in Odoo."""
    with db.session() as s:
        row = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.ticket_id == ticket_id,
                TicketIssueRun.model_name == model_name,
                TicketIssueRun.status == "pending",
            )
            .order_by(TicketIssueRun.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return {"id": row.id, "job_id": row.job_id, "status": row.status}


def get_ticket_issue_run(db: Database, run_id: int) -> dict | None:
    """Return a ticket_issue_runs row as a dict (incl. requeue inputs), or None."""
    with db.session() as s:
        row = s.get(TicketIssueRun, run_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "job_id": row.job_id,
            "ticket_id": row.ticket_id,
            "model_name": row.model_name,
            "github_url": row.github_url,
            "name": row.name,
            "description": row.description,
            "analysis_html": row.analysis_html,
            "planning_basis": row.planning_basis,
            "priority": row.priority,
            "ticket_url": row.ticket_url,
            "status": row.status,
            "issues": row.issues,
            "error_message": row.error_message,
            "model": row.model,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "estimated_cost_usd": float(row.estimated_cost_usd) if row.estimated_cost_usd else None,
            "created_at": row.created_at,
            "completed_at": row.completed_at,
        }


def record_ticket_issue_plan(
    db: Database,
    run_id: int,
    issues: list[dict],
    response: ClaudeResponse,
) -> float:
    """Persist the validated issue plan + Claude usage; returns the estimated cost.

    Runs BEFORE any GitHub call and leaves status 'pending': a partial failure
    must resume from this plan on requeue, never re-plan (a re-plan produces
    different titles and would duplicate the issue set)."""
    with db.session() as s:
        row = s.get(TicketIssueRun, run_id)
        if row is None:
            return 0.0
        row.issues = issues
        row.model = response.model
        row.input_tokens = response.input_tokens
        row.output_tokens = response.output_tokens
        row.cache_read_tokens = response.cache_read_tokens
        row.cache_creation_tokens = response.cache_creation_tokens
        cost = estimate_cost(
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cache_read_tokens=response.cache_read_tokens,
            cache_write_tokens=response.cache_creation_tokens,
        )
        row.estimated_cost_usd = cost
        return cost


def get_latest_ticket_issue_plan(
    db: Database, ticket_id: int, model_name: str, exclude_run_id: int
) -> dict | None:
    """The most recent OTHER run for this record that has a persisted issue
    list, as {"id", "github_url", "issues"} — or None.

    Lets a fresh run (re-click after Odoo's timeout race or a partial failure)
    adopt the prior plan from REVA's own DB instead of trusting GitHub's
    eventually-consistent search: the prior list is authoritative, includes
    not-yet-created items, and is immune to index lag. planning_basis is
    included so the caller can compare bases — a changed consultant docx or
    description (different basis) must NOT adopt the stale plan."""
    with db.session() as s:
        row = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.ticket_id == ticket_id,
                TicketIssueRun.model_name == model_name,
                TicketIssueRun.issues.is_not(None),
                TicketIssueRun.id != exclude_run_id,
            )
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "github_url": row.github_url,
            "issues": row.issues,
            "planning_basis": row.planning_basis,
        }


def update_ticket_issue_progress(db: Database, run_id: int, issues: list[dict]) -> None:
    """Persist creation progress (issue numbers/urls) after each GitHub create.

    Statement-level UPDATE on purpose: this runs once per created issue, and an
    ORM s.get() would drag the full row (ticket text, analysis HTML) across the
    wire each time just to overwrite one column."""
    with db.session() as s:
        s.execute(
            update(TicketIssueRun)
            .where(TicketIssueRun.id == run_id)
            .values(issues=list(issues))
        )


def record_ticket_issue_run_completed(db: Database, run_id: int, issues: list[dict]) -> None:
    """Mark a ticket issue run as completed and store the final issue list."""
    with db.session() as s:
        row = s.get(TicketIssueRun, run_id)
        if row is None:
            return
        row.status = "completed"
        row.issues = list(issues)
        row.completed_at = datetime.now(timezone.utc)


def record_ticket_issue_run_failed(db: Database, run_id: int, error_message: str) -> None:
    """Mark a ticket issue run as failed."""
    with db.session() as s:
        row = s.get(TicketIssueRun, run_id)
        if row is None:
            return
        row.status = "failed"
        row.error_message = error_message
        row.completed_at = datetime.now(timezone.utc)


def reset_ticket_issue_run(db: Database, run_id: int) -> None:
    """Reset a failed/completed run to pending so it can be re-enqueued.

    Keeps `issues` (the persisted plan + progress) so the rerun resumes
    creation and re-sends the callback instead of re-planning."""
    with db.session() as s:
        row = s.get(TicketIssueRun, run_id)
        if row is None:
            return
        row.status = "pending"
        row.error_message = None
        row.completed_at = None
        row.job_id = None


def update_ticket_issue_state(
    db: Database, owner: str, repo: str, number: int, state: str
) -> list[dict]:
    """Set `state` on issue `number` of `owner/repo` across all runs that
    carry it (adopted/reconciled runs share issues), and return the affected
    Odoo records with the NEWEST run's full issue snapshot:
    [{"ticket_id", "model_name", "issues"}].

    Matching is done in Python after a coarse SQL filter: github_url is free
    text from Odoo (casing/.git/trailing-slash variants), so the LIKE only
    narrows the scan and parse_github_repo_url decides.
    """
    from reva.github_urls import parse_github_repo_url

    target = (owner.lower(), repo.lower())
    affected: dict[tuple[int, str], dict] = {}
    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.issues.is_not(None),
                TicketIssueRun.github_url.ilike(f"%github.com/{owner}/{repo}%"),
            )
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
        ).scalars().all()
        for row in rows:
            parsed = parse_github_repo_url(row.github_url)
            if parsed is None or (parsed[0].lower(), parsed[1].lower()) != target:
                continue
            items = [dict(i) for i in (row.issues or [])]
            if not any(i.get("number") == number for i in items):
                continue
            for item in items:
                if item.get("number") == number:
                    item["state"] = state
            row.issues = items
            # rows are newest-first: the first hit per record is its snapshot
            affected.setdefault((row.ticket_id, row.model_name), {
                "ticket_id": row.ticket_id,
                "model_name": row.model_name,
                "issues": items,
            })
    return list(affected.values())


def purge_old_ticket_issue_text(db: Database, older_than_days: int) -> int:
    """Scrub raw ticket inputs on ticket_issue_runs past retention (F1/SECU-8).

    description and analysis_html carry customer-authored content (the
    consultant DOCX is never stored server-side). The issue links in `issues`
    (number/title/url/state) are derived data and kept — but un-created plan
    items on failed runs still hold full Claude-rendered bodies derived from
    that content, so those keys are stripped too (which also means such runs
    can no longer resume; the purge already accepts that trade-off for
    description). Idempotent. Returns the number of rows whose raw text was
    scrubbed."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    with db.session() as s:
        result = s.execute(
            update(TicketIssueRun)
            .where(
                TicketIssueRun.created_at < cutoff,
                TicketIssueRun.description != PURGED_TICKET_TEXT,
            )
            .values(
                description=PURGED_TICKET_TEXT,
                analysis_html=PURGED_TICKET_TEXT,
            )
        )
        rows = s.execute(
            select(TicketIssueRun).where(
                TicketIssueRun.created_at < cutoff,
                TicketIssueRun.issues.is_not(None),
            )
        ).scalars().all()
        for row in rows:
            stripped = [
                {k: v for k, v in item.items() if k not in ("body", "acceptance_criteria")}
                for item in row.issues
            ]
            if stripped != row.issues:
                row.issues = stripped
        return result.rowcount


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
    s.execute(delete(ReviewFinding).where(ReviewFinding.review_run_id == review_run_id))
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
