"""Ticket analysis endpoints.

POST /api/v1/ticket-analysis   — submit a ticket for analysis (fire-and-forget)
GET  /api/v1/ticket-analysis/{analysis_id} — poll for status / result
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.dependencies import get_db
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


@create_router.post(
    "/ticket-analysis",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TicketAnalysisCreated,
)
def submit_ticket_analysis(
    body: TicketAnalysisRequest,
    request: Request,
    db: Database = Depends(get_db),
) -> dict:
    """Accept a ticket text, enqueue the analysis job, and return immediately."""
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
        db, body.ticket_id, body.model_name, body.field_name
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
        ticket_id=body.ticket_id,
        model_name=body.model_name,
        field_name=body.field_name,
        text=body.text,
        attachment=body.attachment,
    )
    analysis_id = writers.record_ticket_analysis_created(db, stub_params)

    # Now build the real params with the correct analysis_id and enqueue.
    params = TicketJobParams(
        analysis_id=analysis_id,
        ticket_id=body.ticket_id,
        model_name=body.model_name,
        field_name=body.field_name,
        text=body.text,
        attachment=body.attachment,
    )
    rq_queue = request.app.state.rq_queue
    job = rq_queue.enqueue(
        "worker.ticket_tasks.run_ticket_analysis",
        params.model_dump(),
        job_timeout=_JOB_TIMEOUT,
    )

    writers.attach_ticket_job_id(db, analysis_id, job.id)

    logger.info("ticket_analysis_enqueued", analysis_id=analysis_id, job_id=job.id)
    return {"analysis_id": analysis_id, "job_id": job.id, "status": "pending"}


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
    """Re-enqueue a failed ticket analysis using the originally submitted text."""
    row = writers.get_ticket_analysis(db, analysis_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Ticket analysis not found")
    if row["status"] not in ("failed", "completed"):
        raise HTTPException(status_code=409, detail="Only failed or completed analyses can be requeued")

    params = TicketJobParams(
        analysis_id=analysis_id,
        ticket_id=row["ticket_id"],
        model_name=row["model_name"],
        field_name=row["field_name"],
        text=row["input_text"],
    )
    writers.reset_ticket_analysis(db, analysis_id)

    rq_queue = request.app.state.rq_queue
    job = rq_queue.enqueue(
        "worker.ticket_tasks.run_ticket_analysis",
        params.model_dump(),
        job_timeout=_JOB_TIMEOUT,
    )
    writers.attach_ticket_job_id(db, analysis_id, job.id)

    logger.info("ticket_analysis_requeued", analysis_id=analysis_id, job_id=job.id)
    return {"analysis_id": analysis_id, "job_id": job.id, "status": "pending"}
