"""Odoo ticket → GitHub issues endpoints (github-issues handoff).

POST /api/v1/create-issues                       — Contract 1: accept, enqueue, 202 {request_id}
GET  /api/v1/ticket-issue-runs                   — paginated runs feed (TUI Tickets tab)
GET  /api/v1/create-issues/{request_id}          — ops/debug: poll status/result
POST /api/v1/create-issues/{request_id}/requeue  — ops: re-run a failed/completed/stale run
                                                   (resumes the persisted plan; the
                                                   callback only lands while Odoo still
                                                   waits on this request_id)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from rq import Retry
from sqlalchemy.exc import IntegrityError

from app.dependencies import get_db, get_github_client, require_odoo_instance, ResolvedOdooInstance
from app.pagination import clamp_limit, clamp_offset
from app.queries import ticket_issues as q
from app.schemas.ticket_issues import (
    CreateIssuesRequest,
    TicketIssueRunPage,
    TicketIssueRunStatus,
    TicketIssueRunSummary,
    TicketIssuesAccepted,
)
from reva.attachment_text import classify_attachment
from reva.db import writers
from reva.db.engine import Database
from reva.errors import PermanentError, TransientError
from reva.github_client import GitHubClient
from reva.github_urls import parse_github_repo_url
from reva.types import TicketIssueJobParams

router = APIRouter()
create_router = APIRouter()  # instance-key gated (see routes/v1/__init__.py)
logger = structlog.get_logger()

_JOB_TIMEOUT = 300  # seconds
# Contract 2's response table mandates retrying the Odoo callback on
# 5xx/network with this backoff; the runner resumes idempotently from its
# persisted plan, so retrying the whole job is safe.
_RETRY = Retry(max=3, interval=[30, 120, 300])
# Failed jobs keep their serialized args (incl. the customer docx) in Redis —
# RQ's default failure TTL is a YEAR, far past the 30-day DB retention purge.
_FAILURE_TTL = 7 * 24 * 3600
# A pending run older than this has no live job (job timeout is 300s plus the
# retry backoff above) — let ops requeue it instead of wedging the ticket.
_STALE_PENDING = timedelta(minutes=30)


def _enqueue(request: Request, db: Database, run_id: int, params: TicketIssueJobParams) -> str:
    """Enqueue the job; on queue failure mark the run failed (so the pending
    dedup doesn't pin future clicks to a row no worker will ever process) and
    surface a 503 — Odoo shows the error and rolls its record back."""
    rq_queue = request.app.state.rq_queue
    try:
        job = rq_queue.enqueue(
            "worker.ticket_issue_tasks.run_ticket_issues",
            params.model_dump(),
            job_timeout=_JOB_TIMEOUT,
            retry=_RETRY,
            failure_ttl=_FAILURE_TTL,
        )
    except Exception as exc:
        writers.record_ticket_issue_run_failed(db, run_id, f"enqueue failed: {exc}")
        logger.error("ticket_issues_enqueue_failed", request_id=run_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job queue unavailable; try again",
        ) from exc
    writers.attach_ticket_issue_job_id(db, run_id, job.id)
    return job.id


@create_router.post(
    "/create-issues",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TicketIssuesAccepted,
)
def submit_create_issues(
    body: CreateIssuesRequest,
    request: Request,
    db: Database = Depends(get_db),
    github: GitHubClient = Depends(get_github_client),
    instance: ResolvedOdooInstance = Depends(require_odoo_instance),
) -> dict:
    """Accept an Odoo create-issues request, enqueue the job, return immediately.

    Odoo's outbound timeout is 10 s and any non-202 is shown to the user with a
    transaction rollback — so validation must happen here, not in the worker.
    """
    parsed = parse_github_repo_url(body.github_url)
    if parsed is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="github_url must be an https://github.com/{owner}/{repo} URL",
        )
    owner, repo = parsed
    # Reachability: the App must be able to reach the repo to create issues
    # there. A definitive "no" (404/forbidden → PermanentError) is rejected so
    # Odoo shows it and rolls back; a GitHub blip (TransientError) is accepted —
    # the worker re-checks before creating issues (ticket_issue_runner), so we
    # don't turn a GitHub outage into a user-facing rejection.
    try:
        github.get_repo_installation_id(owner, repo)
    except PermanentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"github_url is not reachable: REVA's GitHub App is not installed "
                f"on {owner}/{repo} (or the repo does not exist)"
            ),
        ) from exc
    except TransientError:
        logger.warning(
            "create_issues_reachability_transient", github_url=body.github_url, exc_info=True
        )
    if body.description_docx is not None:
        try:
            classify_attachment(
                body.description_docx.filename, body.description_docx.content_base64
            )
        except ValueError as exc:
            # Fail at accept time: Odoo shows the error and rolls back, which
            # beats an async failed-callback for an obviously broken upload.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"description_docx: {exc}",
            ) from exc

    # Dedup: a re-click while a run is still pending returns the SAME
    # request_id, so the in-flight run's callback still matches in Odoo.
    existing = writers.get_pending_ticket_issue_run(db, body.ticket_id, body.model_name, instance.id)
    if existing is not None:
        logger.info(
            "ticket_issues_dedup",
            request_id=existing["id"],
            ticket_id=body.ticket_id,
        )
        return {"request_id": existing["id"], "job_id": existing["job_id"], "status": "pending"}

    # Build a stub with run_id=0 to create the DB row first (the row id IS the
    # request_id), then the real params for the queue.
    stub = TicketIssueJobParams(run_id=0, odoo_instance_id=instance.id, **body.model_dump())
    try:
        run_id = writers.record_ticket_issue_run_created(db, stub)
    except IntegrityError:
        # Two concurrent POSTs raced past the dedup check; the partial unique
        # index (one pending run per record) lost us the race — return the
        # winner's request_id.
        existing = writers.get_pending_ticket_issue_run(db, body.ticket_id, body.model_name, instance.id)
        if existing is not None:
            logger.info("ticket_issues_dedup_race", request_id=existing["id"])
            return {"request_id": existing["id"], "job_id": existing["job_id"], "status": "pending"}
        raise

    params = TicketIssueJobParams(run_id=run_id, odoo_instance_id=instance.id, **body.model_dump())
    job_id = _enqueue(request, db, run_id, params)

    logger.info("ticket_issues_enqueued", request_id=run_id, job_id=job_id)
    return {"request_id": run_id, "job_id": job_id, "status": "pending"}


@router.get(
    "/ticket-issue-runs",
    response_model=TicketIssueRunPage,
)
def list_ticket_issue_runs(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Database = Depends(get_db),
) -> dict:
    """Return a paginated list of create-issues runs (newest first)."""
    limit = clamp_limit(limit, 200)
    offset = clamp_offset(offset)
    items, total = q.list_ticket_issue_runs(db, status=status, limit=limit, offset=offset)
    return {
        "items": [TicketIssueRunSummary.model_validate(i) for i in items],
        "total": total,
    }


@router.get(
    "/create-issues/{request_id}",
    response_model=TicketIssueRunStatus,
)
def get_ticket_issue_run(
    request_id: int,
    db: Database = Depends(get_db),
) -> dict:
    """Return the current status and result of a create-issues run."""
    row = writers.get_ticket_issue_run(db, request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Ticket issue run not found")
    return row


def _is_stale_pending(row: dict) -> bool:
    created_at = row["created_at"]
    if created_at.tzinfo is None:  # SQLite returns naive datetimes
        created_at = created_at.replace(tzinfo=timezone.utc)
    return row["status"] == "pending" and created_at < datetime.now(timezone.utc) - _STALE_PENDING


@router.post(
    "/create-issues/{request_id}/requeue",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TicketIssuesAccepted,
)
def requeue_ticket_issue_run(
    request_id: int,
    request: Request,
    db: Database = Depends(get_db),
) -> dict:
    """Re-enqueue a failed/completed run — or a stale pending one (its job died
    without running, e.g. a SIGKILLed worker; without this the pending dedup
    pins every future click to a dead request_id).

    Resumes the persisted plan (creates only issues without a number) and
    re-sends the callback. Odoo accepts that callback only while the record is
    still pending with this request_id; otherwise the worker logs a 409 and the
    user's re-click path re-links instead.
    """
    row = writers.get_ticket_issue_run(db, request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Ticket issue run not found")
    if row["status"] not in ("failed", "completed") and not _is_stale_pending(row):
        raise HTTPException(
            status_code=409,
            detail="Only failed, completed, or stale pending runs can be requeued",
        )
    if row["description"] == writers.PURGED_TICKET_TEXT and not row["issues"]:
        # No plan persisted and the inputs were purged (SECU-8): a re-run would
        # plan from the sentinel and create garbage issues on GitHub.
        raise HTTPException(
            status_code=409,
            detail="Ticket text purged and no plan persisted; re-trigger from Odoo instead",
        )
    if (row.get("planning_basis") or "").startswith("docx:") and not row["issues"]:
        # An attachment-based run (.docx/.pdf/.txt — all carry the "docx:"
        # basis prefix) that never produced a plan can't be re-planned: the file
        # only ever lived in the (now-gone) job params, not the DB.
        raise HTTPException(
            status_code=409,
            detail="Consultant file not retained and no plan persisted; "
            "re-trigger from Odoo instead",
        )
    other_pending = writers.get_pending_ticket_issue_run(
        db, row["ticket_id"], row["model_name"], row["odoo_instance_id"]
    )
    if other_pending is not None and other_pending["id"] != request_id:
        # The unique pending-per-record index would reject the reset anyway;
        # fail with a meaningful message instead of a 500.
        raise HTTPException(
            status_code=409,
            detail=f"Run {other_pending['id']} is already pending for this record",
        )

    params = TicketIssueJobParams(
        run_id=request_id,
        odoo_instance_id=row["odoo_instance_id"],
        ticket_id=row["ticket_id"],
        model_name=row["model_name"],
        github_url=row["github_url"],
        name=row["name"],
        description=row["description"],
        analysis_html=row["analysis_html"],
        # The consultant document isn't retained server-side; a requeue
        # resumes from the persisted plan (guarded above when there is none).
        description_docx=None,
        priority=row["priority"],
        ticket_url=row["ticket_url"],
        issue_type=row["issue_type"],
    )
    writers.reset_ticket_issue_run(db, request_id)
    job_id = _enqueue(request, db, request_id, params)

    logger.info("ticket_issues_requeued", request_id=request_id, job_id=job_id)
    return {"request_id": request_id, "job_id": job_id, "status": "pending"}
