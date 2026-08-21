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
from sqlalchemy import and_, case, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from reva.cost import estimate_cost
from reva.db.engine import Database
from reva.db.models import (
    AdminAudit,
    AuditFinding,
    ChangeNote,
    ClaudeSpend,
    GithubEvent,
    MutedCategory,
    OdooInstance,
    OpsEvent,
    PendingReview,
    Persona,
    SupportThread,
    SupportTurn,
    PromptVersion,
    PullRequest,
    RepoReviewMemory,
    Repository,
    ReviewFeedback,
    ReviewFinding,
    ReviewRun,
    TicketActual,
    TicketAnalysis,
    TicketIssueReassignment,
    TicketIssueRun,
    TimesheetReviewLine,
    TimesheetReviewRun,
    ValueReport,
)
from reva.types import (
    ClaudeResponse,
    Finding,
    JobParams,
    ReviewResult,
    TicketIssueJobParams,
    TicketJobParams,
    TimesheetJobParams,
    TimesheetLineResult,
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
def claim_review_run(
    db: Database, params: JobParams, job_id: str | None, worker_id: str | None = None
) -> tuple[int, bool]:
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
                worker_id=worker_id,
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
        existing.worker_id = worker_id
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
        # H3: mark the re-review boundary. Recovery scopes to reviews submitted
        # at/after this instant, so the prior attempt's review (same "Run #N"
        # marker, submitted before now) is not mistaken for this attempt's.
        run.reset_at = datetime.now(timezone.utc)


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
        run.triage_escalation = result.triage_escalation
        run.diff_hash = result.diff_hash
        run.delta_base_sha = result.delta_base_sha
        run.carried_from_run_id = result.carried_from_run_id
        run.finding_count = len(result.findings)
        run.intent_check = (
            [v.model_dump() for v in result.intent_check] if result.intent_check else None
        )
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
    db: Database,
    params: JobParams,
    error_class: str,
    message: str,
    cost_usd: float | None = None,
) -> int:
    """Record a failed run. `cost_usd` (M1) captures spend already incurred before
    the failure — e.g. a paid CLI run whose output failed to parse — so the
    rolling budget ledger counts it instead of the failure hiding the charge."""
    with db.session() as s:
        run = _upsert_review_run(s, params, status="failed")
        run.error_class = error_class
        run.error_message = message
        run.completed_at = datetime.now(timezone.utc)
        if cost_usd:
            run.estimated_cost_usd = cost_usd
            _insert_spend(s, "review", cost_usd)
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


def get_review_recovery_since(db: Database, review_run_id: int) -> datetime | None:
    """Lower bound for crash-recovery review lookup, scoping it to this attempt.

    The "Run #N" marker isn't unique across attempts (run-id reuse on a DB reset,
    or a re-review reusing the row), so recovery must ignore a stale prior review
    that shares the marker (PR-9 / H3).

    - Re-reviewed row (reset_at set): return reset_at with NO margin. The reset
      happens after the prior attempt's review was submitted and before this
      attempt posts, so it is an exact boundary; a clock-skew margin here would
      wrongly re-admit the prior review when a re-review follows quickly.
    - First run (reset_at NULL): created_at minus a clock-skew margin. created_at
      precedes the review post by minutes, so the margin only cushions skew and
      cannot reach a prior review (there is none)."""
    with db.session() as s:
        row = s.execute(
            select(ReviewRun.reset_at, ReviewRun.created_at).where(
                ReviewRun.id == review_run_id
            )
        ).one_or_none()
    if row is None:
        return None
    reset_at, created_at = row
    anchor = reset_at if reset_at is not None else created_at
    if anchor is None:
        return None
    # SQLite returns naive datetimes; normalize to UTC-aware so the caller can
    # compare against GitHub's tz-aware submitted_at (Postgres is already aware).
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return anchor if reset_at is not None else anchor - timedelta(minutes=5)


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
    """Posted, still-open findings across ALL completed runs of the PR, oldest first.

    A finding qualifies when it has a github_comment_id (actually posted inline),
    outcome 'open' (not resolved / closed at merge), and carries no 'dismissed'
    feedback. Pass before_run_id to exclude the current run's own findings.

    PR-wide on purpose: the previous version looked only at the single most-recent
    completed run, so any still-open thread from an earlier run became invisible
    the moment a later run completed — including a delta run that found nothing.
    A fix on the second push after a review then never resolved its thread.
    """
    with db.session() as s:
        dismissed = (
            select(ReviewFeedback.id)
            .where(ReviewFeedback.review_finding_id == ReviewFinding.id)
            .where(ReviewFeedback.reaction == "dismissed")
        )
        q = (
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
            .join(ReviewRun, ReviewFinding.review_run_id == ReviewRun.id)
            .where(ReviewRun.pull_request_id == pull_request_id)
            .where(ReviewRun.status == "completed")
            .where(ReviewFinding.github_comment_id.is_not(None))
            .where(ReviewFinding.outcome == "open")
            .where(~dismissed.exists())
            .order_by(ReviewFinding.id.asc())  # oldest first: longest-open threads win the cap
        )
        if before_run_id is not None:
            q = q.where(ReviewRun.id < before_run_id)
        rows = s.execute(q).all()
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


def find_reusable_review(
    db: Database, repository_id: int, diff_hash: str, exclude_pull_request_id: int
) -> dict | None:
    """Most-recent completed, POSTED, full-scope review in this repo whose
    diff_hash matches, on a DIFFERENT PR. diff_hash is NULL on delta runs, so
    the equality filter excludes them. Returns {id, pull_request_id, pr_number}."""
    with db.session() as s:
        row = s.execute(
            select(ReviewRun.id, ReviewRun.pull_request_id, PullRequest.pr_number)
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .where(ReviewRun.repository_id == repository_id)
            .where(ReviewRun.status == "completed")
            .where(ReviewRun.diff_hash == diff_hash)
            .where(ReviewRun.check_run_id.is_not(None))
            .where(ReviewRun.pull_request_id != exclude_pull_request_id)
            .order_by(ReviewRun.completed_at.desc())
            .limit(1)
        ).first()
    if not row:
        return None
    return {"id": row[0], "pull_request_id": row[1], "pr_number": row[2]}


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
                ReviewFinding.review_run_id,
                ReviewFinding.category,
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
        "review_run_id": row[7],
        "category": row[8],
    }


def record_feedback(
    db: Database,
    *,
    review_finding_id: int,
    review_run_id: int,
    github_comment_id: int,
    reactor_login: str,
    reaction: str,
    is_positive: bool,
) -> int | None:
    """Insert one review_feedback row; return its id, or None on a duplicate.

    review_feedback was created (migration 002) for 👍/👎 reactions; it now also
    carries thread-resolution signal, where `reaction` is "resolved"/"unresolved"
    and `is_positive` is the polarity (resolved = accept).

    Idempotent on uq_review_feedback_unique (review_finding_id, reactor_login,
    reaction): a repeated signal (e.g. a redelivered thread-resolved event) is a
    no-op. A stale finding/run id (FK violation) is also swallowed so a webhook
    can't 500 on a deleted finding.
    """
    try:
        with db.session() as s:
            row = ReviewFeedback(
                review_finding_id=review_finding_id,
                review_run_id=review_run_id,
                github_comment_id=github_comment_id,
                reactor_login=reactor_login,
                reaction=reaction,
                is_positive=is_positive,
            )
            s.add(row)
            s.flush()
            return row.id
    except IntegrityError:
        return None


# --- muted categories (Tier 3) -----------------------------------------------


def set_category_mute(
    db: Database, repository_id: int, category: str, muted_by: str, active: bool
) -> None:
    """Mute (active=True) or unmute (active=False) a finding category for a repo.

    Idempotent upsert on (repository_id, category): a repeated /mute is a no-op,
    and /unmute flips the existing row's `active` rather than deleting it (keeps
    the muted_by/created_at audit trail)."""
    now = datetime.now(timezone.utc)
    with db.session() as s:
        existing = s.execute(
            select(MutedCategory).where(
                (MutedCategory.repository_id == repository_id)
                & (MutedCategory.category == category)
            )
        ).scalar_one_or_none()
        if existing is None:
            s.add(MutedCategory(
                repository_id=repository_id, category=category,
                muted_by=muted_by, active=active,
            ))
        else:
            existing.active = active
            existing.muted_by = muted_by
            existing.updated_at = now


def get_muted_categories(db: Database, repository_id: int) -> set[str]:
    """Return the set of actively-muted finding categories for a repo."""
    with db.session() as s:
        rows = s.execute(
            select(MutedCategory.category).where(
                (MutedCategory.repository_id == repository_id)
                & (MutedCategory.active.is_(True))
            )
        ).all()
    return {r[0] for r in rows}


# --- repo review memory (Tier 3 feature B) -----------------------------------


def record_repo_memory(
    db: Database,
    repository_id: int,
    *,
    items: list[dict],
    content: str,
    source_stats: dict,
    response: ClaudeResponse,
) -> int:
    """Write the next memory version for a repo and deactivate the prior one in
    the same transaction (exactly one active row per repo). Returns the new
    version. content "" is valid — it means "nothing to inject", and writing it
    supersedes an older non-empty version so guidance can't outlive its evidence."""
    with db.session() as s:
        prior = s.execute(
            select(RepoReviewMemory)
            .where(RepoReviewMemory.repository_id == repository_id)
            .order_by(RepoReviewMemory.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        next_version = (prior.version + 1) if prior is not None else 1
        # Deactivate every currently-active row for this repo (normally just one).
        s.execute(
            update(RepoReviewMemory)
            .where(RepoReviewMemory.repository_id == repository_id)
            .where(RepoReviewMemory.active.is_(True))
            .values(active=False)
        )
        s.add(RepoReviewMemory(
            repository_id=repository_id,
            version=next_version,
            content=content,
            items=items,
            source_stats=source_stats,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            estimated_cost_usd=estimate_cost(
                response.model, response.input_tokens, response.output_tokens,
                response.cache_read_tokens, response.cache_creation_tokens,
            ),
            active=True,
        ))
        return next_version


def get_active_memory(db: Database, repository_id: int) -> str | None:
    """Active memory content for a repo, or None when absent or empty ("" means
    the last distillation produced nothing — inject nothing)."""
    with db.session() as s:
        content = s.execute(
            select(RepoReviewMemory.content)
            .where(RepoReviewMemory.repository_id == repository_id)
            .where(RepoReviewMemory.active.is_(True))
            .limit(1)
        ).scalar_one_or_none()
    return content or None


def get_active_memory_row(db: Database, repository_id: int) -> dict | None:
    """Active memory metadata for a repo (API/scheduler): version, content,
    item count, created_at, cost. None when no version exists."""
    with db.session() as s:
        row = s.execute(
            select(RepoReviewMemory)
            .where(RepoReviewMemory.repository_id == repository_id)
            .where(RepoReviewMemory.active.is_(True))
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "version": row.version,
            "content": row.content,
            "item_count": len(row.items or []),
            "created_at": row.created_at,
            "estimated_cost_usd": float(row.estimated_cost_usd) if row.estimated_cost_usd else None,
        }


def repos_due_for_memory_distill(
    db: Database, *, min_dismissals: int = 3, window_days: int = 90
) -> list[int]:
    """Repository ids due for a memory re-distill: at least `min_dismissals`
    dismissals in the trailing window AND newer negative feedback than the
    active memory version's created_at (or no version yet)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    with db.session() as s:
        candidates = s.execute(
            select(
                ReviewRun.repository_id,
                func.count(func.distinct(ReviewFeedback.id)).label("dismissed"),
                func.max(ReviewFeedback.created_at).label("newest"),
            )
            .select_from(ReviewFeedback)
            .join(ReviewFinding, ReviewFeedback.review_finding_id == ReviewFinding.id)
            .join(ReviewRun, ReviewFinding.review_run_id == ReviewRun.id)
            .where(ReviewFeedback.reaction == "dismissed")
            .where(ReviewFeedback.created_at >= cutoff)
            .group_by(ReviewRun.repository_id)
            .having(func.count(func.distinct(ReviewFeedback.id)) >= min_dismissals)
        ).all()
        if not candidates:
            return []
        repo_ids = [c.repository_id for c in candidates]
        active = dict(s.execute(
            select(RepoReviewMemory.repository_id, RepoReviewMemory.created_at)
            .where(RepoReviewMemory.repository_id.in_(repo_ids))
            .where(RepoReviewMemory.active.is_(True))
        ).all())
    due = []
    for c in candidates:
        created = active.get(c.repository_id)
        if created is None or (c.newest is not None and c.newest > created):
            due.append(c.repository_id)
    return due


def set_review_run_learned_memory_version(db: Database, run_id: int, version: int) -> None:
    """Stamp the learned-memory version a review injected (attribution)."""
    with db.session() as s:
        row = s.get(ReviewRun, run_id)
        if row is not None:
            row.learned_memory_version = version


def get_memory_distill_input(
    db: Database, repository_id: int, *, since_days: int = 90, max_dismissed: int = 30
) -> dict:
    """Assemble the distiller's input for one repo over the trailing window:
    per-category finding/dismiss/fix counts, the most recent dismissed findings
    (title/category/severity/file — /dismiss carries no free-text reason), and
    the newest negative-feedback timestamp. Repo-scoped (by repository_id)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    with db.session() as s:
        stat_rows = s.execute(
            select(
                ReviewFinding.category,
                func.count(func.distinct(ReviewFinding.id)).label("findings"),
                func.count(func.distinct(
                    case((ReviewFeedback.is_positive.is_(False), ReviewFeedback.review_finding_id))
                )).label("dismissed"),
                func.count(func.distinct(
                    case((ReviewFinding.outcome == "resolved_by_fix", ReviewFinding.id))
                )).label("resolved_by_fix"),
                func.count(func.distinct(
                    case((ReviewFinding.outcome == "still_open_at_merge", ReviewFinding.id))
                )).label("still_open_at_merge"),
            )
            .select_from(ReviewFinding)
            .join(ReviewRun, ReviewFinding.review_run_id == ReviewRun.id)
            .outerjoin(ReviewFeedback, ReviewFeedback.review_finding_id == ReviewFinding.id)
            .where(ReviewRun.repository_id == repository_id)
            .where(ReviewFinding.created_at >= cutoff)
            .group_by(ReviewFinding.category)
            .order_by(ReviewFinding.category)
        ).all()
        dismissed_rows = s.execute(
            select(
                ReviewFinding.title, ReviewFinding.category,
                ReviewFinding.severity, ReviewFinding.file_path,
                ReviewFeedback.created_at,
            )
            .select_from(ReviewFeedback)
            .join(ReviewFinding, ReviewFeedback.review_finding_id == ReviewFinding.id)
            .join(ReviewRun, ReviewFinding.review_run_id == ReviewRun.id)
            .where(ReviewRun.repository_id == repository_id)
            .where(ReviewFeedback.reaction == "dismissed")
            .where(ReviewFeedback.created_at >= cutoff)
            .order_by(ReviewFeedback.created_at.desc())
            .limit(max_dismissed)
        ).all()
    category_stats = [
        {"category": r.category, "findings": r.findings, "dismissed": r.dismissed,
         "resolved_by_fix": r.resolved_by_fix, "still_open_at_merge": r.still_open_at_merge}
        for r in stat_rows
    ]
    dismissed = [
        {"title": r.title, "category": r.category, "severity": r.severity,
         "file_path": r.file_path}
        for r in dismissed_rows
    ]
    newest_feedback_at = dismissed_rows[0].created_at if dismissed_rows else None
    return {
        "window_days": since_days,
        "category_stats": category_stats,
        "dismissed_findings": dismissed,
        "dismissed_count": sum(c["dismissed"] for c in category_stats),
        "newest_feedback_at": newest_feedback_at,
    }


# --- ticket_analyses writers -------------------------------------------------


def record_ticket_analysis_created(db: Database, params: TicketJobParams) -> int:
    """Insert a pending ticket_analyses row and return its id."""
    with db.session() as s:
        row = TicketAnalysis(
            odoo_instance_id=params.odoo_instance_id,
            ticket_id=params.ticket_id,
            model_name=params.model_name,
            field_name=params.field_name,
            github_url=params.github_url,
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
    result_structured: dict | None = None,
    repo_docs_sections_used: int | None = None,
) -> None:
    """Mark a ticket analysis as completed and store the result."""
    with db.session() as s:
        row = s.get(TicketAnalysis, analysis_id)
        if row is None:
            return
        row.status = "completed"
        row.result_html = result_html
        row.result_structured = result_structured
        row.repo_docs_sections_used = repo_docs_sections_used
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
        # SECU-3: the rolling global cap reads the claude_spend ledger ONLY
        # (sum_estimated_cost_since), so the main analysis call has to land
        # there too — the row's own estimated_cost_usd feeds the per-instance
        # cap, not the global one. Recorded atomically with the completion,
        # like reviews and timesheet reviews; the planner leg is recorded
        # separately by the runner as "ticket_planner".
        _insert_spend(s, "ticket_analysis", row.estimated_cost_usd)


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


def record_ticket_analysis_callback_sent(db: Database, analysis_id: int) -> None:
    """Record a successful Odoo callback: the completed analysis reached Odoo.

    Clears any prior callback_error so a successful retry overwrites the failure.
    """
    with db.session() as s:
        row = s.get(TicketAnalysis, analysis_id)
        if row is None:
            return
        row.callback_sent_at = datetime.now(timezone.utc)
        row.callback_error = None


def record_ticket_analysis_callback_failed(
    db: Database, analysis_id: int, error: str
) -> None:
    """Record a failed Odoo callback: the analysis completed but never reached
    Odoo. Leaves callback_sent_at NULL so the row reads 'not in Odoo'."""
    with db.session() as s:
        row = s.get(TicketAnalysis, analysis_id)
        if row is None:
            return
        row.callback_error = error


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


def purge_old_github_events(db: Database, older_than_days: int) -> int:
    """Delete github_events rows older than `older_than_days` (M14).

    Every webhook delivery — even ignored actions — persists its full payload as
    JSONB (PR titles/bodies, sender logins), so the table becomes the largest in
    the DB and carries the most PII with no retention. Deletion is safe: the rows
    exist only for delivery_id dedup (GitHub never redelivers deliveries this old)
    and ad-hoc debugging. Returns the number of rows deleted."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    with db.session() as s:
        result = s.execute(
            delete(GithubEvent).where(GithubEvent.received_at < cutoff)
        )
        return result.rowcount


def purge_old_claude_spend(db: Database, older_than_days: int) -> int:
    """Delete claude_spend ledger rows older than `older_than_days`.

    The rolling budget cap reads only the trailing 24h; cost dashboards read
    weeks. Past the window the rows are pure unbounded growth. Returns the
    number of rows deleted.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    with db.session() as s:
        result = s.execute(delete(ClaudeSpend).where(ClaudeSpend.created_at < cutoff))
        return result.rowcount


# ------------------------------------------------------------------ ops events


def record_ops_event(
    db: Database,
    component: str,
    severity: str,
    event: str,
    detail: dict | None = None,
) -> None:
    """Persist a caught-and-degraded component error.

    Safe-to-fail by contract: this is called from degradation paths, so an
    ops-log write must never break the operation it observes.
    """
    try:
        with db.session() as s:
            s.add(OpsEvent(
                component=component,
                severity=severity,
                event=event,
                detail=detail,
            ))
    except Exception:
        logger.warning(
            "ops_event_write_failed", component=component, ops_event=event, exc_info=True
        )


def purge_old_ops_events(db: Database, older_than_days: int) -> int:
    """Delete ops_events older than the retention window. Returns rows deleted."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    with db.session() as s:
        result = s.execute(delete(OpsEvent).where(OpsEvent.created_at < cutoff))
        return result.rowcount


def get_pending_ticket_analysis(
    db: Database, ticket_id: int, model_name: str, field_name: str, odoo_instance_id: int
) -> dict | None:
    """Return the pending analysis for (instance, ticket, model, field), or None."""
    with db.session() as s:
        row = s.execute(
            select(TicketAnalysis)
            .where(
                TicketAnalysis.odoo_instance_id == odoo_instance_id,
                TicketAnalysis.ticket_id == ticket_id,
                TicketAnalysis.model_name == model_name,
                TicketAnalysis.field_name == field_name,
                TicketAnalysis.status == "pending",
            )
        ).scalars().first()
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
            "odoo_instance_id": row.odoo_instance_id,
            "ticket_id": row.ticket_id,
            "model_name": row.model_name,
            "field_name": row.field_name,
            "github_url": row.github_url,
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
            "repo_docs_sections_used": row.repo_docs_sections_used,
        }


# ------------------------------------------------------- timesheet reviews


def record_timesheet_run_created(db: Database, params: TimesheetJobParams) -> int:
    """Insert a pending timesheet_review_runs row and return its id."""
    with db.session() as s:
        row = TimesheetReviewRun(
            odoo_instance_id=params.odoo_instance_id,
            request_id=params.request_id,
            status="pending",
            total_lines=len(params.lines),
        )
        s.add(row)
        s.flush()
        return row.id


def attach_timesheet_job_id(db: Database, run_id: int, job_id: str) -> None:
    """Store the RQ job ID on the run row after enqueuing."""
    with db.session() as s:
        row = s.get(TimesheetReviewRun, run_id)
        if row is not None:
            row.job_id = job_id


def get_pending_timesheet_run(
    db: Database, odoo_instance_id: int, request_id: str
) -> dict | None:
    """Return the pending run for (instance, request_id), or None."""
    with db.session() as s:
        row = s.execute(
            select(TimesheetReviewRun).where(
                TimesheetReviewRun.odoo_instance_id == odoo_instance_id,
                TimesheetReviewRun.request_id == request_id,
                TimesheetReviewRun.status == "pending",
            )
        ).scalars().first()
        if row is None:
            return None
        return {
            "id": row.id,
            "job_id": row.job_id,
            "status": row.status,
            "created_at": row.created_at,
        }


def get_timesheet_run(db: Database, run_id: int) -> dict | None:
    """Return a timesheet_review_runs row as a dict, or None."""
    with db.session() as s:
        row = s.get(TimesheetReviewRun, run_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "job_id": row.job_id,
            "odoo_instance_id": row.odoo_instance_id,
            "request_id": row.request_id,
            "status": row.status,
            "total_lines": row.total_lines,
            "ok_count": row.ok_count,
            "rewritten_count": row.rewritten_count,
            "needs_human_count": row.needs_human_count,
            "model": row.model,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "cache_read_tokens": row.cache_read_tokens,
            "cache_creation_tokens": row.cache_creation_tokens,
            "estimated_cost_usd": (
                float(row.estimated_cost_usd) if row.estimated_cost_usd else None
            ),
            "callback_payload": row.callback_payload,
            "callback_sent_at": row.callback_sent_at,
            "error_message": row.error_message,
            "created_at": row.created_at,
            "completed_at": row.completed_at,
        }


def record_timesheet_run_failed(db: Database, run_id: int, error_message: str) -> None:
    """Mark a timesheet run as failed."""
    with db.session() as s:
        row = s.get(TimesheetReviewRun, run_id)
        if row is None:
            return
        row.status = "failed"
        row.error_message = error_message
        row.completed_at = datetime.now(timezone.utc)


def get_timesheet_line_ids(db: Database, run_id: int) -> set[int]:
    """Line ids already recorded for this run."""
    with db.session() as s:
        rows = s.execute(
            select(TimesheetReviewLine.line_id).where(TimesheetReviewLine.run_id == run_id)
        ).scalars().all()
        return set(rows)


def record_timesheet_chunk(
    db: Database,
    run_id: int,
    results: list[TimesheetLineResult],
    responses: list[ClaudeResponse],
) -> None:
    """Persist one processed chunk atomically."""
    with db.session() as s:
        run = s.get(TimesheetReviewRun, run_id)
        if run is None:
            return
        for result in results:
            s.add(TimesheetReviewLine(
                run_id=run_id,
                line_id=result.line_id,
                status=result.status,
                reason=result.reason,
            ))
        payload = dict(run.callback_payload or {"results": []})
        entries = list(payload.get("results", []))
        for result in results:
            if result.status == "rewritten":
                entries.append({
                    "line_id": result.line_id,
                    "status": result.status,
                    "updated_desc": result.updated_desc,
                })
            elif result.status == "needs_human":
                entries.append({
                    "line_id": result.line_id,
                    "status": result.status,
                    "reason": result.reason,
                })
        payload["results"] = entries
        run.callback_payload = payload

        for response in responses:
            run.model = response.model
            run.input_tokens += response.input_tokens
            run.output_tokens += response.output_tokens
            run.cache_read_tokens += response.cache_read_tokens
            run.cache_creation_tokens += response.cache_creation_tokens
            cost = estimate_cost(
                response.model,
                response.input_tokens,
                response.output_tokens,
                response.cache_read_tokens,
                response.cache_creation_tokens,
            )
            run.estimated_cost_usd = float(run.estimated_cost_usd or 0.0) + cost
            _insert_spend(s, "timesheet_review", cost)


def record_timesheet_run_completed(db: Database, run_id: int) -> None:
    """Mark the run completed; counts are derived from line rows."""
    with db.session() as s:
        run = s.get(TimesheetReviewRun, run_id)
        if run is None:
            return
        rows = s.execute(
            select(TimesheetReviewLine.status).where(TimesheetReviewLine.run_id == run_id)
        ).scalars().all()
        run.ok_count = sum(1 for status in rows if status == "ok")
        run.rewritten_count = sum(1 for status in rows if status == "rewritten")
        run.needs_human_count = sum(1 for status in rows if status == "needs_human")
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)


def record_timesheet_callback_sent(db: Database, run_id: int) -> None:
    """Record callback success and clear the payload."""
    with db.session() as s:
        row = s.get(TimesheetReviewRun, run_id)
        if row is None:
            return
        row.callback_sent_at = datetime.now(timezone.utc)
        row.callback_payload = None


# --- ticket_issue_runs writers -------------------------------------------------


def compute_planning_basis(params: TicketIssueJobParams) -> str:
    """Content-addressed digest of WHAT a run plans from.

    "docx:<sha1[:16]>" when a consultant file is attached (.docx/.pdf/.txt — its
    content is the basis), else "text:<sha1[:16]>" over description + analysis.
    The "docx:" prefix is kept for any attachment (not just .docx) so the dedup
    digest and the GitHub marker stay stable across the .pdf/.txt rollout; it
    lets requeue tell an attachment run apart without keeping the file, and the
    hash lets a re-run detect a revised spec. NOT a security hash — stability
    across a run and its requeues is the only requirement."""
    if params.description_docx is not None:
        key = "docx\x00" + params.description_docx.content_base64
        prefix = "docx:"
    else:
        key = "text\x00" + params.description + "\x00" + params.analysis_html
        prefix = "text:"
    digest = hashlib.sha1(  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
        key.encode(), usedforsecurity=False
    ).hexdigest()[:16]
    basis = prefix + digest
    if params.issue_type:
        # A typed request plans separately from an untyped one over the same
        # text (own marker, no cross-adoption); untyped runs keep the pre-type
        # basis format so existing markers stay valid.
        return params.issue_type.lower() + ":" + basis
    return basis


def _normalize_repo_full_name(github_url: str) -> str | None:
    """Lowercased "owner/repo" for `github_url`, or None if unparseable (M15)."""
    from reva.github_urls import parse_github_repo_url

    parsed = parse_github_repo_url(github_url)
    if parsed is None:
        return None
    return f"{parsed[0]}/{parsed[1]}".lower()


def record_ticket_issue_run_created(db: Database, params: TicketIssueJobParams) -> int:
    """Insert a pending ticket_issue_runs row and return its id.

    The row id doubles as the request_id Odoo stores and the callback echoes
    (github-issues handoff, Contracts 1+2)."""
    with db.session() as s:
        row = TicketIssueRun(
            ticket_id=params.ticket_id,
            model_name=params.model_name,
            odoo_instance_id=params.odoo_instance_id,
            github_url=params.github_url,
            repo_full_name=_normalize_repo_full_name(params.github_url),
            name=params.name,
            description=params.description,
            analysis_html=params.analysis_html,
            planning_basis=compute_planning_basis(params),
            issue_type=params.issue_type,
            github_username=params.github_username,
            priority=params.priority,
            ticket_url=params.ticket_url,
            github_project_url=params.github_project_url,
            plan_date=params.plan_date,
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
    db: Database, ticket_id: int, model_name: str, odoo_instance_id: int | None = None
) -> dict | None:
    """Return the most recent pending run for this record, or None.

    Request dedup: a re-click while a run is in flight gets the SAME
    request_id back, so the in-flight run's callback still matches in Odoo."""
    with db.session() as s:
        filters = [
            TicketIssueRun.ticket_id == ticket_id,
            TicketIssueRun.model_name == model_name,
            TicketIssueRun.status == "pending",
        ]
        if odoo_instance_id is not None:
            filters.append(TicketIssueRun.odoo_instance_id == odoo_instance_id)
        row = s.execute(
            select(TicketIssueRun)
            .where(*filters)
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
            "odoo_instance_id": row.odoo_instance_id,
            "github_url": row.github_url,
            "name": row.name,
            "description": row.description,
            "analysis_html": row.analysis_html,
            "planning_basis": row.planning_basis,
            "issue_type": row.issue_type,
            "github_username": row.github_username,
            "priority": row.priority,
            "ticket_url": row.ticket_url,
            "github_project_url": row.github_project_url,
            "plan_date": row.plan_date,
            "status": row.status,
            "issues": row.issues,
            "plan_summary": row.plan_summary,
            "parent_issue": row.parent_issue,
            "error_message": row.error_message,
            "model": row.model,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "estimated_cost_usd": float(row.estimated_cost_usd) if row.estimated_cost_usd else None,
            "created_at": row.created_at,
            "completed_at": row.completed_at,
        }


def get_latest_structured_analysis(
    db: Database, odoo_instance_id: int | None, ticket_id: int, model_name: str
) -> dict | None:
    """Latest completed structured ticket analysis for a record, if present."""
    with db.session() as s:
        filters = [
            TicketAnalysis.ticket_id == ticket_id,
            TicketAnalysis.model_name == model_name,
            TicketAnalysis.status == "completed",
            TicketAnalysis.result_structured.is_not(None),
        ]
        if odoo_instance_id is None:
            filters.append(TicketAnalysis.odoo_instance_id.is_(None))
        else:
            filters.append(TicketAnalysis.odoo_instance_id == odoo_instance_id)
        row = s.execute(
            select(TicketAnalysis)
            .where(*filters)
            .order_by(TicketAnalysis.completed_at.desc(), TicketAnalysis.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return row.result_structured if row is not None else None


@_retry_on_conflict
def get_or_create_change_note(
    db: Database,
    repo_full_name: str,
    pr_number: int,
    ticket_id: int,
    odoo_instance_id: int,
    model_name: str,
    pr_title: str = "",
    pr_url: str = "",
) -> tuple[int, dict]:
    """Return the deduped change-note row for a PR/ticket, creating it if absent.

    pr_title/pr_url are stored on creation so the batched change-summary can
    render the PR ref later without a GitHub round-trip.
    """
    repo_full_name = repo_full_name.lower()
    with db.session() as s:
        row = s.execute(
            select(ChangeNote).where(
                ChangeNote.repo_full_name == repo_full_name,
                ChangeNote.pr_number == pr_number,
                ChangeNote.ticket_id == ticket_id,
            )
        ).scalar_one_or_none()
        if row is None:
            row = ChangeNote(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                ticket_id=ticket_id,
                odoo_instance_id=odoo_instance_id,
                model_name=model_name,
                status="pending",
                pr_title=pr_title,
                pr_url=pr_url,
            )
            s.add(row)
            s.flush()
        return row.id, _change_note_dict(row)


def _change_note_dict(row: ChangeNote) -> dict:
    return {
        "id": row.id,
        "repo_full_name": row.repo_full_name,
        "pr_number": row.pr_number,
        "ticket_id": row.ticket_id,
        "odoo_instance_id": row.odoo_instance_id,
        "model_name": row.model_name,
        "status": row.status,
        "note_html": row.note_html,
        "pr_title": row.pr_title,
        "pr_url": row.pr_url,
        "error_message": row.error_message,
        "estimated_cost_usd": (
            float(row.estimated_cost_usd) if row.estimated_cost_usd is not None else None
        ),
        "created_at": row.created_at,
        "completed_at": row.completed_at,
        "delivered_at": row.delivered_at,
    }


def record_change_note_completed(
    db: Database, note_id: int, note_html: str, cost: float
) -> None:
    with db.session() as s:
        row = s.get(ChangeNote, note_id)
        if row is None:
            return
        row.status = "completed"
        row.note_html = note_html
        row.error_message = None
        row.estimated_cost_usd = cost
        row.completed_at = datetime.now(timezone.utc)


def record_change_note_failed(
    db: Database, note_id: int, status: str, error: str
) -> None:
    if status not in ("failed", "skipped_budget"):
        raise ValueError("change note status must be failed or skipped_budget")
    with db.session() as s:
        row = s.get(ChangeNote, note_id)
        if row is None:
            return
        row.status = status
        row.error_message = error
        row.completed_at = datetime.now(timezone.utc)


def has_pending_change_notes(
    db: Database, odoo_instance_id: int, ticket_id: int, model_name: str
) -> bool:
    """True while any note for the ticket is still generating (status 'pending').
    'pending' is the only non-terminal status; completed/failed/skipped_budget
    all count as done. Blocks change-summary delivery until every note lands."""
    with db.session() as s:
        row = s.execute(
            select(ChangeNote.id).where(
                ChangeNote.odoo_instance_id == odoo_instance_id,
                ChangeNote.ticket_id == ticket_id,
                ChangeNote.model_name == model_name,
                ChangeNote.status == "pending",
            ).limit(1)
        ).first()
        return row is not None


def get_undelivered_change_notes(
    db: Database, odoo_instance_id: int, ticket_id: int, model_name: str
) -> list[dict]:
    """Completed, not-yet-delivered notes for the ticket, oldest PR first.
    Failed / budget-skipped rows (no note_html) are excluded from the batch."""
    with db.session() as s:
        rows = s.execute(
            select(ChangeNote).where(
                ChangeNote.odoo_instance_id == odoo_instance_id,
                ChangeNote.ticket_id == ticket_id,
                ChangeNote.model_name == model_name,
                ChangeNote.status == "completed",
                ChangeNote.note_html.is_not(None),
                ChangeNote.delivered_at.is_(None),
            ).order_by(ChangeNote.pr_number.asc())
        ).scalars().all()
        return [
            {
                "id": row.id,
                "repo_full_name": row.repo_full_name,
                "pr_number": row.pr_number,
                "pr_title": row.pr_title,
                "pr_url": row.pr_url,
                "note_html": row.note_html,
            }
            for row in rows
        ]


def mark_change_notes_delivered(db: Database, note_ids: list[int]) -> None:
    """Stamp delivered_at on the shipped rows in one update (idempotent: a
    re-run over already-stamped ids is a no-op via the undelivered filter)."""
    if not note_ids:
        return
    with db.session() as s:
        s.execute(
            update(ChangeNote)
            .where(ChangeNote.id.in_(note_ids))
            .values(delivered_at=datetime.now(timezone.utc))
        )


def upsert_value_report(
    db: Database,
    period_start,
    period_end,
    content_md: str,
    stats: dict,
) -> int:
    """One row per period; a re-run replaces content and resets chat_sent."""
    with db.session() as s:
        row = s.execute(
            select(ValueReport).where(
                ValueReport.period_start == period_start,
                ValueReport.period_end == period_end,
            )
        ).scalar_one_or_none()
        if row is None:
            row = ValueReport(
                period_start=period_start,
                period_end=period_end,
                content_md=content_md,
                stats=stats,
                chat_sent=False,
            )
            s.add(row)
            s.flush()
        else:
            row.content_md = content_md
            row.stats = stats
            row.chat_sent = False
            row.created_at = datetime.now(timezone.utc)
        return row.id


def set_value_report_chat_sent(db: Database, report_id: int) -> None:
    with db.session() as s:
        row = s.get(ValueReport, report_id)
        if row is not None:
            row.chat_sent = True


def get_value_reports(db: Database, limit: int = 12) -> list[dict]:
    with db.session() as s:
        rows = s.execute(
            select(ValueReport)
            .order_by(ValueReport.period_start.desc(), ValueReport.id.desc())
            .limit(limit)
        ).scalars().all()
        return [{
            "id": row.id,
            "period_start": row.period_start,
            "period_end": row.period_end,
            "content_md": row.content_md,
            "stats": row.stats or {},
            "chat_sent": row.chat_sent,
            "created_at": row.created_at,
        } for row in rows]


def record_ticket_issue_plan(
    db: Database,
    run_id: int,
    issues: list[dict],
    response: ClaudeResponse,
    summary: str = "",
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
        if summary:
            row.plan_summary = summary
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


def set_ticket_issue_parent(db: Database, run_id: int, parent: dict) -> None:
    """Persist the parent ("epic") issue for a run. Statement-level UPDATE for
    the same reason as update_ticket_issue_progress: avoid dragging the full
    row (ticket text) over the wire to set one column."""
    with db.session() as s:
        s.execute(
            update(TicketIssueRun)
            .where(TicketIssueRun.id == run_id)
            .values(parent_issue=dict(parent))
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


# ---------------------------------------------- issue-ownership overrides
# Which Odoo record owns a REVA-created issue is otherwise implicit in
# ticket_issue_runs.issues. These four functions are the ONLY way that implicit
# answer gets corrected — every query that resolves an owner must consult them,
# or a moved issue silently bounces back to the record it was moved off.


def record_issue_reassignment(
    db: Database,
    *,
    odoo_instance_id: int,
    repo_full_name: str,
    number: int,
    ticket_id: int,
    model_name: str,
) -> None:
    """Upsert the override for one issue. Last call wins — an issue moved twice
    ends up owned by the last target, and `from` never enters the key."""
    repo = repo_full_name.lower()
    with db.session() as s:
        row = s.execute(
            select(TicketIssueReassignment).where(
                TicketIssueReassignment.odoo_instance_id == odoo_instance_id,
                TicketIssueReassignment.repo_full_name == repo,
                TicketIssueReassignment.number == number,
            )
        ).scalar_one_or_none()
        if row is None:
            s.add(TicketIssueReassignment(
                odoo_instance_id=odoo_instance_id,
                repo_full_name=repo,
                number=number,
                ticket_id=ticket_id,
                model_name=model_name,
            ))
            return
        row.ticket_id = ticket_id
        row.model_name = model_name


def clear_issue_reassignment(
    db: Database, *, odoo_instance_id: int, repo_full_name: str, number: int
) -> None:
    """Drop the override, restoring the runs' own answer. Used when an issue is
    moved back to its natural owner — writing an identity override instead
    would leave a row that means nothing and has to be read past forever."""
    repo = repo_full_name.lower()
    with db.session() as s:
        s.query(TicketIssueReassignment).filter_by(
            odoo_instance_id=odoo_instance_id, repo_full_name=repo, number=number
        ).delete()


def issue_owner_overrides(
    db: Database,
    odoo_instance_id: int | None,
    repo_full_name: str,
    numbers: list[int],
) -> dict[int, tuple[int, str]]:
    """{number: (ticket_id, model_name)} for the overridden numbers only.

    A NULL instance is a pre-multi-instance run, which can never carry an
    override (the endpoint that writes them is instance-gated) — it resolves to
    nothing rather than matching every row.
    """
    if odoo_instance_id is None or not numbers:
        return {}
    repo = repo_full_name.lower()
    with db.session() as s:
        rows = s.execute(
            select(
                TicketIssueReassignment.number,
                TicketIssueReassignment.ticket_id,
                TicketIssueReassignment.model_name,
            ).where(
                TicketIssueReassignment.odoo_instance_id == odoo_instance_id,
                TicketIssueReassignment.repo_full_name == repo,
                TicketIssueReassignment.number.in_(numbers),
            )
        ).all()
        return {r.number: (r.ticket_id, r.model_name) for r in rows}


def issues_moved_onto(
    db: Database, odoo_instance_id: int | None, ticket_id: int, model_name: str
) -> list[tuple[str, int]]:
    """[(repo_full_name, number)] moved ONTO this record.

    The direction that cannot be derived from the record's own runs: a target
    may have no ticket_issue_runs row at all, which is exactly the case a naive
    implementation drops.
    """
    if odoo_instance_id is None:
        return []
    with db.session() as s:
        rows = s.execute(
            select(
                TicketIssueReassignment.repo_full_name,
                TicketIssueReassignment.number,
            ).where(
                TicketIssueReassignment.odoo_instance_id == odoo_instance_id,
                TicketIssueReassignment.ticket_id == ticket_id,
                TicketIssueReassignment.model_name == model_name,
            ).order_by(
                TicketIssueReassignment.repo_full_name,
                TicketIssueReassignment.number,
            )
        ).all()
        return [(r.repo_full_name, r.number) for r in rows]


def update_ticket_issue_state(
    db: Database, owner: str, repo: str, number: int, state: str,
    closed_at: str | None = None,
) -> list[dict]:
    """Set `state` on issue `number` of `owner/repo` across all runs that
    carry it (adopted/reconciled runs share issues), and return the affected
    Odoo records with the NEWEST run's full issue snapshot:
    [{"ticket_id", "model_name", "issues"}].

    `complete_date` is stamped from `closed_at` (UTC date, YYYY-MM-DD) when the
    issue closes and cleared to None on reopen — per-issue, alongside `state`.

    Matched on the normalized repo_full_name column (indexed) instead of a
    leading-wildcard github_url ILIKE that full-scanned the table; the big text
    columns are deferred (load_only) since only issues/ids are needed (M15).
    """
    from sqlalchemy.orm import load_only

    target = f"{owner.lower()}/{repo.lower()}"
    affected: dict[tuple[int, str], dict] = {}
    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.issues.is_not(None),
                TicketIssueRun.repo_full_name == target,
            )
            .options(load_only(
                TicketIssueRun.ticket_id,
                TicketIssueRun.model_name,
                TicketIssueRun.odoo_instance_id,
                TicketIssueRun.issues,
                TicketIssueRun.created_at,
            ))
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
        ).scalars().all()
        for row in rows:
            items = [dict(i) for i in (row.issues or [])]
            if not any(i.get("number") == number for i in items):
                continue
            complete_date = (closed_at or "")[:10] or None if state == "closed" else None
            for item in items:
                if item.get("number") == number:
                    item["state"] = state
                    item["complete_date"] = complete_date
            row.issues = items
            # rows are newest-first: the first hit per record is its snapshot
            affected.setdefault((row.ticket_id, row.model_name), {
                "ticket_id": row.ticket_id,
                "model_name": row.model_name,
                "odoo_instance_id": row.odoo_instance_id,
                "issues": items,
            })

    # Reassignment (spec 2026-08-20): the per-issue state writes above are
    # unchanged — state is a fact about the issue, and the plan lives on
    # whichever run created it. Only WHO WE TELL changes. The source is
    # deliberately not notified: its union no longer carries the issue, so
    # nothing about it changed.
    redirected: dict[tuple[int, str], dict] = {}
    for record in affected.values():
        override_owner = issue_owner_overrides(
            db, record["odoo_instance_id"], target, [number]
        ).get(number)
        ticket_id, model_name = override_owner or (
            record["ticket_id"], record["model_name"]
        )
        redirected.setdefault((ticket_id, model_name), {
            "ticket_id": ticket_id,
            "model_name": model_name,
            "odoo_instance_id": record["odoo_instance_id"],
            "issues": record["issues"],
        })
    return list(redirected.values())


def _instance_filter(odoo_instance_id: int | None):
    if odoo_instance_id is None:
        return TicketIssueRun.odoo_instance_id.is_(None)
    return TicketIssueRun.odoo_instance_id == odoo_instance_id


def update_ticket_issue_estimate(
    db: Database, odoo_instance_id: int | None, ticket_id: int, model_name: str,
    number: int, estimate_hours: float | None,
) -> dict | None:
    """Set `estimate_hours` on issue `number` across all of the record's runs
    (adopted/reconciled runs share issues, and the union feeds later
    issues-created callbacks — a single-row update would resurrect the old
    value).

    Returns the board target from the newest run that placed the issue on a
    Projects board — {"github_url", "github_project_url", "project_item_id"} —
    or the same dict with a None project_item_id/github_project_url when the
    issue was never placed, or None when no run carries the issue at all."""
    from sqlalchemy.orm import load_only

    # Reassignment (spec 2026-08-20): Odoo addresses the estimate at the record
    # the issue sits on NOW, but the run holding the issue still belongs to the
    # record it came from. Widen the search to that run's record for issues an
    # override moved onto this one.
    owners: list[tuple[int, str]] = [(ticket_id, model_name)]
    for repo_full_name, moved_number in issues_moved_onto(
        db, odoo_instance_id, ticket_id, model_name
    ):
        if moved_number != number:
            continue
        source = natural_issue_owner(db, odoo_instance_id, repo_full_name, number)
        if source is not None and source not in owners:
            owners.append(source)

    target: dict | None = None
    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun)
            .where(
                or_(*[
                    and_(TicketIssueRun.ticket_id == t, TicketIssueRun.model_name == m)
                    for t, m in owners
                ]),
                _instance_filter(odoo_instance_id),
                TicketIssueRun.issues.is_not(None),
            )
            .options(load_only(
                TicketIssueRun.github_url,
                TicketIssueRun.github_project_url,
                TicketIssueRun.issues,
                TicketIssueRun.created_at,
            ))
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
        ).scalars().all()
        for row in rows:
            items = [dict(i) for i in (row.issues or [])]
            hit = next((i for i in items if i.get("number") == number), None)
            if hit is None:
                continue
            for item in items:
                if item.get("number") == number:
                    item["estimate_hours"] = estimate_hours
            row.issues = items
            if target is None:
                target = {
                    "github_url": row.github_url,
                    "github_project_url": None,
                    "project_item_id": None,
                }
            # A project_item_id is only meaningful with the board it was
            # created on — take both from the same (newest such) run.
            if (
                target["project_item_id"] is None
                and row.github_project_url
                and hit.get("project_item_id")
            ):
                target["github_project_url"] = row.github_project_url
                target["project_item_id"] = hit["project_item_id"]
    return target


def _union_item(item: dict) -> dict:
    """Project a stored plan item onto the documented union shape."""
    return {
        "number": item.get("number"),
        "title": item.get("title", ""),
        "url": item.get("url"),
        "state": item.get("state") or "open",
        "plan_date": item.get("plan_date"),
        "complete_date": item.get("complete_date"),
        "estimate_hours": item.get("estimate_hours"),
    }


def _overrides_away(
    db: Database,
    odoo_instance_id: int | None,
    ticket_id: int,
    model_name: str,
    numbers: list[int],
) -> set[int]:
    """Of `numbers` (all carried by this record's runs), those an override has
    moved to a DIFFERENT record. An override pointing back at this record is
    not a move and must not drop the issue."""
    if odoo_instance_id is None:
        return set()
    repos = _repos_for_record(db, odoo_instance_id, ticket_id, model_name)
    moved: set[int] = set()
    for repo in repos:
        for number, owner in issue_owner_overrides(
            db, odoo_instance_id, repo, numbers
        ).items():
            if owner != (ticket_id, model_name):
                moved.add(number)
    return moved


def _repos_for_record(
    db: Database, odoo_instance_id: int | None, ticket_id: int, model_name: str
) -> list[str]:
    """Distinct lowercased repos this record has runs for."""
    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun.repo_full_name)
            .where(
                TicketIssueRun.ticket_id == ticket_id,
                TicketIssueRun.model_name == model_name,
                _instance_filter(odoo_instance_id),
                TicketIssueRun.repo_full_name.is_not(None),
            )
            .distinct()
        ).all()
        return [r.repo_full_name for r in rows]


def _issue_item_from_runs(
    db: Database, repo_full_name: str, number: int
) -> dict | None:
    """The newest run's copy of issue `number` on `repo_full_name`, in union
    shape. None when no run carries it — a reassignment may name an issue REVA
    does not know yet, and that must not fabricate an entry."""
    from sqlalchemy.orm import load_only

    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.repo_full_name == repo_full_name.lower(),
                TicketIssueRun.issues.is_not(None),
            )
            .options(load_only(TicketIssueRun.issues, TicketIssueRun.created_at))
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
        ).scalars().all()
        for row in rows:
            for item in row.issues or []:
                if item.get("number") == number:
                    return _union_item(item)
    return None


def natural_issue_owner(
    db: Database, odoo_instance_id: int | None, repo_full_name: str, number: int
) -> tuple[int, str] | None:
    """(ticket_id, model_name) of the newest run carrying `number` — the issue's
    owner BEFORE any override. None when no run carries it, which is how the
    reassign endpoint tells "unknown issue" from "known issue moved".

    Instance-scoped (fix round 1): an override can only ever address records
    in the caller's own instance, so a run belonging to a different instance
    — or a legacy NULL-instance run — must never answer this. Unfiltered, a
    same-numbered issue owned by another instance could satisfy the "moving
    back to the natural owner" check and get a caller's override silently
    cleared, or feed a coincidentally-matching (ticket_id, model_name) into a
    same-instance query elsewhere (update_ticket_issue_estimate's widened
    owners list)."""
    from sqlalchemy.orm import load_only

    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.repo_full_name == repo_full_name.lower(),
                _instance_filter(odoo_instance_id),
                TicketIssueRun.issues.is_not(None),
            )
            .options(load_only(
                TicketIssueRun.ticket_id,
                TicketIssueRun.model_name,
                TicketIssueRun.issues,
                TicketIssueRun.created_at,
            ))
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
        ).scalars().all()
        for row in rows:
            if any(i.get("number") == number for i in (row.issues or [])):
                return row.ticket_id, row.model_name
    return None


def get_ticket_issue_union(
    db: Database, odoo_instance_id: int | None, ticket_id: int, model_name: str
) -> list[dict]:
    """Union of created issues across ALL runs for this record, deduped by
    issue number (newest run wins title/url/state), sorted by number.

    The Odoo issues-created handler replaces the record's whole issue list
    with the payload — sending only the completing run's issues would wipe
    what earlier requests created (wizard + planner requests accumulate).
    Parents are excluded (parent_issue column, never in `issues`).

    Reassignment (spec 2026-08-20) moves numbers between records without
    touching any run: numbers moved AWAY are dropped here, and numbers moved
    ONTO this record are pulled in from whichever run still holds their plan.
    That second direction is why this cannot be a pure per-record query — the
    target may have no run of its own at all.
    """
    from sqlalchemy.orm import load_only

    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.ticket_id == ticket_id,
                TicketIssueRun.model_name == model_name,
                _instance_filter(odoo_instance_id),
                TicketIssueRun.issues.is_not(None),
            )
            .options(load_only(TicketIssueRun.issues, TicketIssueRun.created_at))
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
        ).scalars().all()
        seen: dict[int, dict] = {}
        for row in rows:  # newest first — first occurrence of a number wins
            for item in row.issues or []:
                n = item.get("number")
                if n is None or n in seen:
                    continue
                seen[n] = _union_item(item)

    # Drop what moved away. Computed after the loop so the repo key comes from
    # the runs themselves rather than being threaded through the query.
    if seen:
        moved_away = _overrides_away(db, odoo_instance_id, ticket_id, model_name,
                                    list(seen))
        for number in moved_away:
            seen.pop(number, None)

    # Pull in what moved on, from whichever run still holds the plan.
    for repo_full_name, number in issues_moved_onto(
        db, odoo_instance_id, ticket_id, model_name
    ):
        if number in seen:
            continue
        item = _issue_item_from_runs(db, repo_full_name, number)
        if item is not None:
            seen[number] = item

    return sorted(seen.values(), key=lambda i: i["number"])


def get_board_items_for_issues(
    db: Database, repo_full_name: str, issue_numbers: list[int]
) -> list[dict]:
    """Open REVA-created issues among `issue_numbers` that sit on a Projects
    board: [{number, project_item_id, github_project_url}]. The newest run's
    occurrence of a number decides (mirrors get_ticket_issue_union's
    newest-wins dedup) — a closed newest occurrence is skipped even if an
    older run still shows it open. Runs without a board URL and items without
    a persisted project_item_id never match (board-status spec 2026-07-10)."""
    if not issue_numbers:
        return []
    wanted = set(issue_numbers)
    repo = repo_full_name.lower()
    out: dict[int, dict] = {}
    seen: set[int] = set()
    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.repo_full_name == repo,
                TicketIssueRun.issues.is_not(None),
                TicketIssueRun.github_project_url.is_not(None),
            )
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
        ).scalars().all()
        for row in rows:
            for item in row.issues or []:
                n = item.get("number")
                if n is None or n not in wanted or n in seen:
                    continue
                seen.add(n)  # newest occurrence decides, even when skipped
                if item.get("state") == "closed" or not item.get("project_item_id"):
                    continue
                out[n] = {
                    "number": n,
                    "project_item_id": item["project_item_id"],
                    "github_project_url": row.github_project_url,
                }
    return sorted(out.values(), key=lambda i: i["number"])


def _issues_all_closed(issues: list[dict]) -> bool:
    return bool(issues) and all(item.get("state") == "closed" for item in issues)


def list_ready_tickets(db: Database, limit: int = 10) -> list[dict]:
    """Tickets whose union of REVA-created issues is non-empty and all closed."""
    from sqlalchemy.orm import load_only

    candidates: dict[tuple[int | None, int, str], dict] = {}
    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun)
            .where(TicketIssueRun.issues.is_not(None))
            .options(load_only(
                TicketIssueRun.odoo_instance_id,
                TicketIssueRun.ticket_id,
                TicketIssueRun.model_name,
                TicketIssueRun.repo_full_name,
                TicketIssueRun.name,
                TicketIssueRun.issues,
                TicketIssueRun.created_at,
            ))
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
        ).scalars().all()
        for row in rows:
            key = (row.odoo_instance_id, row.ticket_id, row.model_name)
            candidates.setdefault(key, {
                "odoo_instance_id": row.odoo_instance_id,
                "ticket_id": row.ticket_id,
                "model_name": row.model_name,
                "repo_full_name": row.repo_full_name,
                "name": row.name,
            })

    # Reassignment (spec 2026-08-20): candidates come from run rows, so a record
    # whose only issues arrived by a move would never be considered ready.
    with db.session() as s:
        moved = s.execute(
            select(
                TicketIssueReassignment.odoo_instance_id,
                TicketIssueReassignment.ticket_id,
                TicketIssueReassignment.model_name,
                TicketIssueReassignment.repo_full_name,
            ).distinct()
        ).all()
    for row in moved:
        key = (row.odoo_instance_id, row.ticket_id, row.model_name)
        candidates.setdefault(key, {
            "odoo_instance_id": row.odoo_instance_id,
            "ticket_id": row.ticket_id,
            "model_name": row.model_name,
            "repo_full_name": row.repo_full_name,
            "name": "",
        })

    ready: list[dict] = []
    for (odoo_instance_id, ticket_id, model_name), meta in candidates.items():
        issues = get_ticket_issue_union(db, odoo_instance_id, ticket_id, model_name)
        if not _issues_all_closed(issues):
            continue
        ready.append({**meta, "issue_count": len(issues), "issues": issues})
        if len(ready) >= limit:
            break
    return ready


def count_ready_tickets(db: Database) -> int:
    return len(list_ready_tickets(db, limit=10_000))


def get_latest_ticket_issue_parent(
    db: Database,
    odoo_instance_id: int | None,
    ticket_id: int,
    model_name: str,
    repo_full_name: str,
    exclude_run_id: int,
) -> dict | None:
    """The record's existing parent ("epic") issue in this repo, from the most
    recent other run that has one — or None. One epic per ticket: a new run
    attaches its issues to this parent instead of creating a second one."""
    from sqlalchemy.orm import load_only

    with db.session() as s:
        row = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.ticket_id == ticket_id,
                TicketIssueRun.model_name == model_name,
                TicketIssueRun.repo_full_name == repo_full_name,
                _instance_filter(odoo_instance_id),
                TicketIssueRun.parent_issue.is_not(None),
                TicketIssueRun.id != exclude_run_id,
            )
            .options(load_only(TicketIssueRun.parent_issue, TicketIssueRun.created_at))
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return dict(row.parent_issue) if row is not None else None


def purge_old_ticket_issue_text(db: Database, older_than_days: int) -> int:
    """Scrub raw ticket inputs on ticket_issue_runs past retention (F1/SECU-8).

    description and analysis_html carry customer-authored content (the
    consultant DOCX is never stored server-side); plan_summary is a
    Claude-rendered summary of that same ticket text, so it is nulled too.
    The issue links in `issues` (number/title/url/state/dates/estimate) are
    derived data and kept — but un-created plan items on failed runs still hold
    full Claude-rendered bodies derived from that content, so those keys are
    stripped too (which also means such runs can no longer resume; the purge
    already accepts that trade-off for description). Idempotent. Returns the
    number of rows whose raw text was scrubbed."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    with db.session() as s:
        # Strip issue bodies FIRST, while description still marks the row
        # un-purged, so a later run (description already the sentinel) skips it
        # instead of re-loading every historical row each day.
        rows = s.execute(
            select(TicketIssueRun).where(
                TicketIssueRun.created_at < cutoff,
                TicketIssueRun.description != PURGED_TICKET_TEXT,
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
        # synchronize_session=False: the issue rows are already loaded in this
        # session, and the default evaluator would choke comparing their (naive,
        # on SQLite) created_at to the aware cutoff. We only need the rowcount.
        result = s.execute(
            update(TicketIssueRun)
            .where(
                TicketIssueRun.created_at < cutoff,
                TicketIssueRun.description != PURGED_TICKET_TEXT,
            )
            .values(
                description=PURGED_TICKET_TEXT,
                analysis_html=PURGED_TICKET_TEXT,
                plan_summary=None,
            )
            .execution_options(synchronize_session=False),
        )
        return result.rowcount


# --- ticket actuals (estimate-calibration loop C1) ---------------------------


def record_ticket_actuals(
    db: Database,
    odoo_instance_id: int,
    ticket_id: int,
    model_name: str,
    actual_hours: float,
    timesheet_line_count: int | None = None,
) -> None:
    """Upsert the timesheet totals Odoo pushed for a done ticket.

    One row per (instance, ticket): a re-done ticket re-sends its totals and
    the latest push wins; reported_at is bumped on every push.
    """
    with db.session() as s:
        row = s.execute(
            select(TicketActual).where(
                TicketActual.odoo_instance_id == odoo_instance_id,
                TicketActual.ticket_id == ticket_id,
                TicketActual.model_name == model_name,
            )
        ).scalar_one_or_none()
        if row is None:
            s.add(TicketActual(
                odoo_instance_id=odoo_instance_id,
                ticket_id=ticket_id,
                model_name=model_name,
                actual_hours=actual_hours,
                timesheet_line_count=timesheet_line_count,
            ))
        else:
            row.actual_hours = actual_hours
            row.timesheet_line_count = timesheet_line_count
            row.reported_at = datetime.now(timezone.utc)


def get_ticket_actuals(
    db: Database, odoo_instance_id: int, ticket_id: int, model_name: str
) -> dict | None:
    with db.session() as s:
        row = s.execute(
            select(TicketActual).where(
                TicketActual.odoo_instance_id == odoo_instance_id,
                TicketActual.ticket_id == ticket_id,
                TicketActual.model_name == model_name,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "actual_hours": float(row.actual_hours),
            "timesheet_line_count": row.timesheet_line_count,
            "reported_at": row.reported_at,
        }


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


def create_odoo_instance(
    db: Database,
    *,
    name: str,
    key_hash: str,
    key_prefix: str,
    callback_url: str,
    callback_api_key_enc: str,
    odoo_version: str | None = None,
) -> int:
    """Insert an odoo_instances row and return its id."""
    with db.session() as s:
        row = OdooInstance(
            name=name,
            key_hash=key_hash,
            key_prefix=key_prefix,
            callback_url=callback_url,
            callback_api_key_enc=callback_api_key_enc,
            odoo_version=odoo_version,
        )
        s.add(row)
        s.flush()
        return row.id


def get_odoo_instance(db: Database, instance_id: int) -> dict | None:
    """Return an odoo_instances row as a dict (incl. callback config), or None."""
    with db.session() as s:
        row = s.get(OdooInstance, instance_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "name": row.name,
            "key_prefix": row.key_prefix,
            "key_hash": row.key_hash,
            "callback_url": row.callback_url,
            "callback_api_key_enc": row.callback_api_key_enc,
            "active": row.active,
            "is_default": row.is_default,
            "daily_budget_usd": (
                float(row.daily_budget_usd) if row.daily_budget_usd is not None else None
            ),
            "rate_limit_per_minute": row.rate_limit_per_minute,
            "odoo_version": row.odoo_version,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


def rotate_odoo_instance_key(
    db: Database, instance_id: int, *, key_hash: str, key_prefix: str
) -> bool:
    """Replace the inbound key hash/prefix. Returns False if the row is missing."""
    with db.session() as s:
        row = s.get(OdooInstance, instance_id)
        if row is None:
            return False
        row.key_hash = key_hash
        row.key_prefix = key_prefix
        row.updated_at = datetime.now(timezone.utc)
        return True


def update_odoo_instance(db: Database, instance_id: int, **fields: object) -> bool:
    """Update mutable Odoo instance fields. Returns False if missing."""
    allowed = {
        "name",
        "callback_url",
        "callback_api_key_enc",
        "active",
        "daily_budget_usd",
        "rate_limit_per_minute",
        "odoo_version",
    }
    with db.session() as s:
        row = s.get(OdooInstance, instance_id)
        if row is None:
            return False
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"update_odoo_instance: unknown field {key!r}")
            setattr(row, key, value)
        row.updated_at = datetime.now(timezone.utc)
        return True


def delete_odoo_instance(db: Database, instance_id: int) -> bool:
    """Hard-delete an Odoo instance. Returns False if the row is missing.

    Run history keeps its data but loses the instance link: the nullable
    odoo_instance_id FKs are set NULL, and change_notes rows (NOT NULL FK)
    are deleted with the instance.
    """
    with db.session() as s:
        row = s.get(OdooInstance, instance_id)
        if row is None:
            return False
        for model in (TicketAnalysis, TicketIssueRun, TimesheetReviewRun):
            s.execute(
                update(model)
                .where(model.odoo_instance_id == instance_id)
                .values(odoo_instance_id=None)
            )
        s.execute(delete(ChangeNote).where(ChangeNote.odoo_instance_id == instance_id))
        s.delete(row)
        return True


def sum_instance_cost_since(db: Database, odoo_instance_id: int, since: datetime) -> float:
    """Rolling spend (USD) for one Odoo instance across its run tables.

    Extension point: when new instance-scoped paid paths land, add their tables
    here so every per-instance budget gate reads one source.
    """
    total = 0.0
    with db.session() as s:
        for model in (TicketAnalysis, TicketIssueRun, TimesheetReviewRun, SupportTurn):
            value = s.execute(
                select(func.coalesce(func.sum(model.estimated_cost_usd), 0)).where(
                    model.odoo_instance_id == odoo_instance_id,
                    model.created_at >= since,
                )
            ).scalar_one()
            total += float(value)
    return total


# ------------------------------------------------------------------- personas

_PERSONA_FIELDS = (
    "language",
    "formality",
    "technical_depth",
    "length",
    "salutation",
    "sign_off",
    "style_notes",
    "content_policy",
    "active",
)


def _persona_key(repo_full_name: str) -> str:
    """Normalise "Owner/Repo" the way repo_doc_sections does, so a persona is
    found regardless of the casing Odoo happens to send in github_url."""
    return repo_full_name.strip().lower()


def _persona_to_dict(row: Persona) -> dict:
    out = {"id": row.id, "scope": row.scope, "repo_full_name": row.repo_full_name}
    out.update({field: getattr(row, field) for field in _PERSONA_FIELDS})
    return out


def upsert_persona(
    db: Database, *, scope: str, repo_full_name: str | None = None, **fields
) -> int:
    """Create or replace the persona for `scope` (+ repo). Returns its id.

    Only the knobs passed are written; the rest stay NULL so the resolver can
    inherit them from the default row (per-field resolution, not whole-row).
    """
    unknown = set(fields) - set(_PERSONA_FIELDS)
    if unknown:
        raise ValueError(f"unknown persona field(s): {sorted(unknown)}")
    key = _persona_key(repo_full_name) if repo_full_name else None
    with db.session() as s:
        row = s.execute(
            select(Persona).where(Persona.scope == scope, Persona.repo_full_name == key)
        ).scalar_one_or_none()
        if row is None:
            row = Persona(scope=scope, repo_full_name=key)
            s.add(row)
        for field, value in fields.items():
            setattr(row, field, value)
        row.updated_at = datetime.now(timezone.utc)
        s.flush()
        return row.id


def get_default_persona(db: Database) -> dict | None:
    """The fallback persona used when a request names no repo, or names one
    with no persona of its own."""
    with db.session() as s:
        row = s.execute(
            select(Persona).where(Persona.scope == "default")
        ).scalar_one_or_none()
        return _persona_to_dict(row) if row is not None else None


def get_repo_persona(db: Database, repo_full_name: str) -> dict | None:
    with db.session() as s:
        row = s.execute(
            select(Persona).where(
                Persona.scope == "repo",
                Persona.repo_full_name == _persona_key(repo_full_name),
            )
        ).scalar_one_or_none()
        return _persona_to_dict(row) if row is not None else None


def list_personas(db: Database) -> list[dict]:
    """All personas, default first — the order the TUI/API render them in."""
    with db.session() as s:
        # 'default' sorts before 'repo' ascending, which is the order we want.
        rows = s.execute(
            select(Persona).order_by(Persona.scope.asc(), Persona.repo_full_name)
        ).scalars().all()
        return [_persona_to_dict(row) for row in rows]


# ------------------------------------------------- support threads and turns

_SUPPORT_TURN_FIELDS = (
    "id", "thread_id", "odoo_instance_id", "seq", "job_id", "question",
    "answer_html", "result_structured", "request_kind", "answer_status",
    "grounding_level", "status", "error_message", "model", "estimated_cost_usd",
    "created_at", "completed_at", "callback_sent_at", "callback_error",
    "image_count",
)


def _support_turn_to_dict(row: SupportTurn) -> dict:
    return {field: getattr(row, field) for field in _SUPPORT_TURN_FIELDS}


def get_or_create_support_thread(
    db: Database,
    *,
    odoo_instance_id: int | None,
    ticket_id: int,
    model_name: str,
    field_name: str,
    github_url: str | None = None,
    persona_snapshot: dict | None = None,
) -> int:
    """Return the thread id for this Odoo record, creating it on first contact.

    Keyed including field_name so two delivery targets on one record don't
    collide (mirrors idx_ticket_analyses_pending).

    `github_url` is re-synced from every request, clearing included. Odoo owns
    the repo link and a consultant can change or remove it between turns — a
    thread that kept whatever URL it was born with once answered a question
    about one system out of another system's clone, and `requeue` rebuilds its
    params from this row, so a stale URL would resurrect the wrong repo.
    """
    with db.session() as s:
        row = s.execute(
            select(SupportThread).where(
                SupportThread.odoo_instance_id == odoo_instance_id,
                SupportThread.ticket_id == ticket_id,
                SupportThread.model_name == model_name,
                SupportThread.field_name == field_name,
            )
        ).scalar_one_or_none()
        if row is None:
            row = SupportThread(
                odoo_instance_id=odoo_instance_id,
                ticket_id=ticket_id,
                model_name=model_name,
                field_name=field_name,
                github_url=github_url,
                persona_snapshot=persona_snapshot,
            )
            s.add(row)
            s.flush()
        elif row.github_url != github_url:
            row.github_url = github_url
        return row.id


def _support_thread_to_dict(row: SupportThread) -> dict:
    return {
        "id": row.id, "odoo_instance_id": row.odoo_instance_id,
        "ticket_id": row.ticket_id, "model_name": row.model_name,
        "field_name": row.field_name, "github_url": row.github_url,
        "status": row.status, "created_at": row.created_at,
        "last_turn_at": row.last_turn_at,
    }


def get_support_thread(db: Database, thread_id: int) -> dict | None:
    with db.session() as s:
        row = s.get(SupportThread, thread_id)
        return _support_thread_to_dict(row) if row is not None else None


def list_support_threads(db: Database, limit: int = 50) -> list[dict]:
    with db.session() as s:
        rows = s.execute(
            select(SupportThread).order_by(SupportThread.created_at.desc()).limit(limit)
        ).scalars().all()
        return [_support_thread_to_dict(r) for r in rows]


def record_support_turn_created(
    db: Database,
    thread_id: int,
    odoo_instance_id: int | None,
    question: str,
    image_count: int = 0,
) -> int:
    """Open a pending turn, assigning the next seq in the thread."""
    with db.session() as s:
        highest = s.execute(
            select(func.coalesce(func.max(SupportTurn.seq), 0)).where(
                SupportTurn.thread_id == thread_id
            )
        ).scalar_one()
        row = SupportTurn(
            thread_id=thread_id,
            odoo_instance_id=odoo_instance_id,
            seq=highest + 1,
            question=question,
            image_count=image_count,
        )
        s.add(row)
        s.flush()
        return row.id


def get_support_turn(db: Database, turn_id: int) -> dict | None:
    with db.session() as s:
        row = s.get(SupportTurn, turn_id)
        return _support_turn_to_dict(row) if row is not None else None


def get_pending_support_turn(db: Database, thread_id: int) -> dict | None:
    """Backs the submit dedup: a re-click while a turn is in flight returns the
    same turn instead of enqueuing a second paid job."""
    with db.session() as s:
        row = s.execute(
            select(SupportTurn).where(
                SupportTurn.thread_id == thread_id, SupportTurn.status == "pending"
            )
        ).scalar_one_or_none()
        return _support_turn_to_dict(row) if row is not None else None


def attach_support_job_id(db: Database, turn_id: int, job_id: str) -> None:
    with db.session() as s:
        row = s.get(SupportTurn, turn_id)
        if row is not None:
            row.job_id = job_id


def record_support_turn_completed(
    db: Database,
    turn_id: int,
    answer_html: str,
    response: ClaudeResponse,
    result_structured: dict | None,
    request_kind: str | None,
    answer_status: str | None,
    grounding_level: str | None,
) -> None:
    """Persist the answer and its cost.

    Spend is recorded in the claude_spend ledger atomically with the row, the
    same way reviews and ticket analyses do it — the global rolling cap reads
    only that ledger, while the per-instance cap reads estimated_cost_usd via
    sum_instance_cost_since.
    """
    with db.session() as s:
        row = s.get(SupportTurn, turn_id)
        if row is None:
            return
        row.status = "completed"
        row.answer_html = answer_html
        row.result_structured = result_structured
        row.request_kind = request_kind
        row.answer_status = answer_status
        row.grounding_level = grounding_level
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
        thread = s.get(SupportThread, row.thread_id)
        if thread is not None:
            thread.last_turn_at = row.completed_at
            # The thread mirrors its latest turn. Without this it sits at the
            # 'open' default forever, which reads as "nothing happened" on a
            # thread that has actually answered.
            thread.status = "answered"
        _insert_spend(s, "support_answer", row.estimated_cost_usd)


def record_support_turn_failed(db: Database, turn_id: int, error: str) -> None:
    with db.session() as s:
        row = s.get(SupportTurn, turn_id)
        if row is not None:
            row.status = "failed"
            row.error_message = error[:2000]
            row.completed_at = datetime.now(timezone.utc)
            thread = s.get(SupportThread, row.thread_id)
            if thread is not None:
                thread.status = "failed"
                thread.last_turn_at = row.completed_at


def reset_support_turn(db: Database, turn_id: int) -> None:
    """Requeue: back to pending, clearing the previous outcome but keeping the
    question and seq so thread ordering is stable."""
    with db.session() as s:
        row = s.get(SupportTurn, turn_id)
        if row is None:
            return
        row.status = "pending"
        row.error_message = None
        row.completed_at = None
        row.callback_sent_at = None
        row.callback_error = None


def record_support_turn_callback_sent(db: Database, turn_id: int) -> None:
    with db.session() as s:
        row = s.get(SupportTurn, turn_id)
        if row is not None:
            row.callback_sent_at = datetime.now(timezone.utc)
            row.callback_error = None


def record_support_turn_callback_failed(db: Database, turn_id: int, error: str) -> None:
    with db.session() as s:
        row = s.get(SupportTurn, turn_id)
        if row is not None:
            row.callback_error = error[:2000]


def list_support_turns(db: Database, thread_id: int, limit: int = 50) -> list[dict]:
    """Every turn on a thread, oldest first — the drill-down view.

    Distinct from `prior_support_turns`, which is the prompt-replay query: that
    one filters to completed turns before a given seq. This one shows the
    operator everything, failures included.
    """
    with db.session() as s:
        rows = s.execute(
            select(SupportTurn)
            .where(SupportTurn.thread_id == thread_id)
            .order_by(SupportTurn.seq.asc())
            .limit(limit)
        ).scalars().all()
        return [_support_turn_to_dict(row) for row in rows]


def prior_support_turns(
    db: Database,
    thread_id: int,
    before_seq: int,
    exclude_question: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Turns that are genuinely CONVERSATION HISTORY, oldest-first for replay.

    Two filters, both learned the hard way — without them a thread of retries
    reads as a conversation and the model answers "nothing new since my
    previous replies":

    - **Delivered only** (`callback_sent_at` set). An answer whose callback
      failed was never seen by anyone, so it is not shared context; replaying
      it makes the model refer to something the consultant never received.
    - **Not the same question.** Pressing "Support request" again after a
      failure creates a new turn with IDENTICAL text. That is a retry, not a
      follow-up, and replaying it teaches the model to restate its earlier
      answer instead of answering afresh.

    Ordering is chronological because the model reads them as a conversation;
    the current turn is excluded so it can't be replayed as its own history.
    """
    with db.session() as s:
        conditions = [
            SupportTurn.thread_id == thread_id,
            SupportTurn.seq < before_seq,
            SupportTurn.status == "completed",
            SupportTurn.callback_sent_at.is_not(None),
        ]
        if exclude_question is not None:
            conditions.append(SupportTurn.question != exclude_question)
        rows = s.execute(
            select(SupportTurn)
            .where(*conditions)
            .order_by(SupportTurn.seq.asc())
            .limit(limit)
        ).scalars().all()
        return [_support_turn_to_dict(row) for row in rows]
