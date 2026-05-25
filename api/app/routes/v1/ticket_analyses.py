"""Ticket analysis endpoints.

POST /api/v1/ticket-analysis   — submit a ticket for analysis (fire-and-forget)
GET  /api/v1/ticket-analysis/{analysis_id} — poll for status / result
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.dependencies import get_db
from app.queries import ticket_analyses as q
from app.schemas.ticket_analyses import (
    TicketAnalysisCreated,
    TicketAnalysisPage,
    TicketAnalysisRequest,
    TicketAnalysisSummary,
    TicketAnalysisStatus,
)
from reva.db import writers
from reva.db.engine import Database
from reva.types import TicketJobParams

router = APIRouter()
logger = structlog.get_logger()

_JOB_TIMEOUT = 300  # seconds


@router.post(
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
    )
    analysis_id = writers.record_ticket_analysis_created(db, stub_params)

    # Now build the real params with the correct analysis_id and enqueue.
    params = TicketJobParams(
        analysis_id=analysis_id,
        ticket_id=body.ticket_id,
        model_name=body.model_name,
        field_name=body.field_name,
        text=body.text,
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
