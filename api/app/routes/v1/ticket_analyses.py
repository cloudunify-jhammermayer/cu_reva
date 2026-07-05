"""Ticket analysis endpoints.

POST /api/v1/ticket-analysis   — submit a ticket for analysis (fire-and-forget)
GET  /api/v1/ticket-analysis/{analysis_id} — poll for status / result
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from rq import Retry
from sqlalchemy.exc import IntegrityError

from app.dependencies import (
    ResolvedOdooInstance,
    assert_instance_within_budget,
    get_db,
    require_odoo_instance,
)
from app.pagination import clamp_limit, clamp_offset
from app.queries import ticket_analyses as q
from app.schemas.ticket_analyses import (
    TicketAnalysisCreated,
    TicketAnalysisPage,
    TicketAnalysisRequest,
    TicketAnalysisSummary,
    TicketAnalysisStatus,
)
from reva.attachment_text import classify_attachment
from reva.db import writers
from reva.db.engine import Database
from reva.types import TicketJobParams

router = APIRouter()
create_router = APIRouter()  # instance-key gated (see routes/v1/__init__.py)
logger = structlog.get_logger()

_JOB_TIMEOUT = 300  # seconds
# The runner re-raises TransientError expecting RQ to retry; without retry= a
# single blip permanently strands the analysis (row stuck pending, Odoo stuck
# "pending"). The runner resumes idempotently (reuses persisted HTML), so
# retrying the whole job never re-pays Claude for a callback-only failure (H7).
_RETRY = Retry(max=3, interval=[30, 120, 300])
# Failed jobs keep their serialized args (incl. the customer ticket text and
# any base64 attachment) in Redis; requeue rebuilds params from the DB row, so
# retention buys nothing past debugging. 24h keeps redis noeviction headroom.
_FAILURE_TTL = 24 * 3600
# A pending row older than this has no live job (timeout 300s + retry backoff) —
# let ops requeue it instead of the dedup wedging the ticket forever (H6).
_STALE_PENDING = timedelta(minutes=30)


def _enqueue(request: Request, db: Database, analysis_id: int, params: TicketJobParams) -> str:
    """Enqueue the analysis job; on queue failure mark the row failed (so the
    pending dedup doesn't pin future submits to a row no worker will process)
    and surface a 503 so Odoo shows the error."""
    rq_queue = request.app.state.rq_queue
    try:
        job = rq_queue.enqueue(
            "worker.ticket_tasks.run_ticket_analysis",
            params.model_dump(),
            job_timeout=_JOB_TIMEOUT,
            retry=_RETRY,
            failure_ttl=_FAILURE_TTL,
        )
    except Exception as exc:
        writers.record_ticket_analysis_failed(db, analysis_id, f"enqueue failed: {exc}")
        logger.error("ticket_analysis_enqueue_failed", analysis_id=analysis_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job queue unavailable; try again",
        ) from exc
    writers.attach_ticket_job_id(db, analysis_id, job.id)
    return job.id


def _is_stale_pending(row: dict) -> bool:
    created_at = row["created_at"]
    if created_at.tzinfo is None:  # SQLite returns naive datetimes
        created_at = created_at.replace(tzinfo=timezone.utc)
    return row["status"] == "pending" and created_at < datetime.now(timezone.utc) - _STALE_PENDING


@create_router.post(
    "/ticket-analysis",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TicketAnalysisCreated,
)
def submit_ticket_analysis(
    body: TicketAnalysisRequest,
    request: Request,
    db: Database = Depends(get_db),
    instance: ResolvedOdooInstance = Depends(require_odoo_instance),
) -> dict:
    """Accept a ticket text, enqueue the analysis job, and return immediately."""
    assert_instance_within_budget(db, instance)
    if body.attachment is not None:
        try:
            classify_attachment(body.attachment.filename, body.attachment.content_base64)
        except ValueError as exc:
            # Accept-time 422 is ticket-analysis's only error channel to Odoo
            # (worker failures only land in the DB), so reject unsupported or
            # malformed attachments here while Odoo can still show the error.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"attachment: {exc}",
            ) from exc
    # Dedup: if a pending analysis already exists for this record, return it.
    existing = writers.get_pending_ticket_analysis(
        db, body.ticket_id, body.model_name, body.field_name, instance.id
    )
    if existing is not None:
        logger.info(
            "ticket_analysis_dedup",
            analysis_id=existing["id"],
            ticket_id=body.ticket_id,
        )
        return {"analysis_id": existing["id"], "job_id": existing["job_id"], "status": "pending"}

    # Build a stub TicketJobParams with analysis_id=0 to create the DB row first.
    stub_params = TicketJobParams(
        analysis_id=0,
        odoo_instance_id=instance.id,
        ticket_id=body.ticket_id,
        model_name=body.model_name,
        field_name=body.field_name,
        text=body.text,
        attachment=body.attachment,
    )
    try:
        analysis_id = writers.record_ticket_analysis_created(db, stub_params)
    except IntegrityError:
        # Two concurrent POSTs raced past the dedup check; the partial unique
        # index (one pending analysis per record) lost us the race — return the
        # winner's id instead of creating a second row and a second paid job.
        existing = writers.get_pending_ticket_analysis(
            db, body.ticket_id, body.model_name, body.field_name, instance.id
        )
        if existing is not None:
            logger.info("ticket_analysis_dedup_race", analysis_id=existing["id"])
            return {"analysis_id": existing["id"], "job_id": existing["job_id"], "status": "pending"}
        raise

    # Now build the real params with the correct analysis_id and enqueue.
    params = TicketJobParams(
        analysis_id=analysis_id,
        odoo_instance_id=instance.id,
        ticket_id=body.ticket_id,
        model_name=body.model_name,
        field_name=body.field_name,
        text=body.text,
        attachment=body.attachment,
    )
    job_id = _enqueue(request, db, analysis_id, params)

    logger.info("ticket_analysis_enqueued", analysis_id=analysis_id, job_id=job_id)
    return {"analysis_id": analysis_id, "job_id": job_id, "status": "pending"}


@router.get(
    "/ticket-analyses",
    response_model=TicketAnalysisPage,
)
def list_ticket_analyses(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Database = Depends(get_db),
) -> dict:
    """Return a paginated list of ticket analyses."""
    limit = clamp_limit(limit, 200)
    offset = clamp_offset(offset)
    items, total = q.list_ticket_analyses(db, status=status, limit=limit, offset=offset)
    return {
        "items": [TicketAnalysisSummary.model_validate(i) for i in items],
        "total": total,
    }


@router.get(
    "/ticket-analysis/{analysis_id}",
    response_model=TicketAnalysisStatus,
)
def get_ticket_analysis(
    analysis_id: int,
    db: Database = Depends(get_db),
) -> dict:
    """Return the current status and result of a ticket analysis job."""
    row = writers.get_ticket_analysis(db, analysis_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Ticket analysis not found")
    return row


@router.post(
    "/ticket-analysis/{analysis_id}/requeue",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TicketAnalysisCreated,
)
def requeue_ticket_analysis(
    analysis_id: int,
    request: Request,
    db: Database = Depends(get_db),
) -> dict:
    """Re-enqueue a failed/completed analysis — or a stale pending one whose job
    died without running (e.g. a SIGKILLed worker or a lost Redis job); without
    this the pending dedup pins every future submit to a dead analysis_id."""
    row = writers.get_ticket_analysis(db, analysis_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Ticket analysis not found")
    if row["status"] not in ("failed", "completed") and not _is_stale_pending(row):
        raise HTTPException(
            status_code=409,
            detail="Only failed, completed, or stale pending analyses can be requeued",
        )
    other_pending = writers.get_pending_ticket_analysis(
        db, row["ticket_id"], row["model_name"], row["field_name"], row["odoo_instance_id"]
    )
    if other_pending is not None and other_pending["id"] != analysis_id:
        # The unique pending-per-record index would reject the reset; fail with a
        # meaningful message instead of a 500.
        raise HTTPException(
            status_code=409,
            detail=f"Analysis {other_pending['id']} is already pending for this record",
        )

    params = TicketJobParams(
        analysis_id=analysis_id,
        odoo_instance_id=row["odoo_instance_id"],
        ticket_id=row["ticket_id"],
        model_name=row["model_name"],
        field_name=row["field_name"],
        text=row["input_text"],
    )
    writers.reset_ticket_analysis(db, analysis_id)
    job_id = _enqueue(request, db, analysis_id, params)

    logger.info("ticket_analysis_requeued", analysis_id=analysis_id, job_id=job_id)
    return {"analysis_id": analysis_id, "job_id": job_id, "status": "pending"}
