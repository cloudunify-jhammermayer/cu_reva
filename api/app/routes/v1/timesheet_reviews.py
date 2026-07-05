"""Timesheet wording review endpoints.

POST /api/v1/timesheet-review      — accept a batch from Odoo and enqueue review
GET  /api/v1/timesheet-reviews     — list runs for the admin/TUI surface
GET  /api/v1/timesheet-review/{id} — inspect one run
"""

import structlog
from datetime import datetime, timedelta, timezone
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
from app.queries import timesheet_reviews as q
from app.schemas.timesheet_reviews import (
    TimesheetReviewCreated,
    TimesheetReviewPage,
    TimesheetReviewRequest,
    TimesheetReviewStatus,
    TimesheetReviewSummary,
)
from reva.db import writers
from reva.db.engine import Database
from reva.types import TIMESHEET_CHUNK_SIZE, TimesheetJobParams

router = APIRouter()
create_router = APIRouter()
logger = structlog.get_logger()

_RETRY = Retry(max=3, interval=[60, 300, 900])
_FAILURE_TTL = 7 * 24 * 3600
_STALE_PENDING = timedelta(minutes=60)


def _job_timeout(line_count: int) -> int:
    n_chunks = max(1, (line_count + TIMESHEET_CHUNK_SIZE - 1) // TIMESHEET_CHUNK_SIZE)
    return max(600, 120 * n_chunks)


def _is_stale_pending(row: dict) -> bool:
    created_at = row["created_at"]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at < datetime.now(timezone.utc) - _STALE_PENDING


def _enqueue(request: Request, db: Database, run_id: int, params: TimesheetJobParams) -> str:
    rq_queue = request.app.state.rq_queue
    try:
        job = rq_queue.enqueue(
            "worker.timesheet_tasks.run_timesheet_review",
            params.model_dump(),
            job_timeout=_job_timeout(len(params.lines)),
            retry=_RETRY,
            failure_ttl=_FAILURE_TTL,
        )
    except Exception as exc:
        writers.record_timesheet_run_failed(db, run_id, f"enqueue failed: {exc}")
        logger.error("timesheet_review_enqueue_failed", run_id=run_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job queue unavailable; try again",
        ) from exc
    writers.attach_timesheet_job_id(db, run_id, job.id)
    return job.id


@create_router.post(
    "/timesheet-review",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TimesheetReviewCreated,
)
def submit_timesheet_review(
    body: TimesheetReviewRequest,
    request: Request,
    db: Database = Depends(get_db),
    instance: ResolvedOdooInstance = Depends(require_odoo_instance),
) -> dict:
    assert_instance_within_budget(db, instance)
    for word in body.flagged_words:
        if len(word) > 100:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="flagged_words entries must be 100 characters or fewer",
            )

    existing = writers.get_pending_timesheet_run(db, instance.id, body.request_id)
    if existing is not None and not _is_stale_pending(existing):
        logger.info("timesheet_review_dedup", run_id=existing["id"])
        return {"run_id": existing["id"], "job_id": existing["job_id"], "status": "pending"}
    if existing is not None:
        writers.record_timesheet_run_failed(
            db, existing["id"], "stale pending run superseded by re-submit"
        )
        logger.warning("timesheet_review_stale_superseded", run_id=existing["id"])

    stub = TimesheetJobParams(
        run_id=0,
        odoo_instance_id=instance.id,
        request_id=body.request_id,
        flagged_words=body.flagged_words,
        lines=body.lines,
    )
    try:
        run_id = writers.record_timesheet_run_created(db, stub)
    except IntegrityError:
        existing = writers.get_pending_timesheet_run(db, instance.id, body.request_id)
        if existing is not None:
            logger.info("timesheet_review_dedup_race", run_id=existing["id"])
            return {"run_id": existing["id"], "job_id": existing["job_id"], "status": "pending"}
        raise

    params = TimesheetJobParams(
        run_id=run_id,
        odoo_instance_id=instance.id,
        request_id=body.request_id,
        flagged_words=body.flagged_words,
        lines=body.lines,
    )
    job_id = _enqueue(request, db, run_id, params)
    logger.info("timesheet_review_enqueued", run_id=run_id, job_id=job_id)
    return {"run_id": run_id, "job_id": job_id, "status": "pending"}


@router.get("/timesheet-reviews", response_model=TimesheetReviewPage)
def list_timesheet_reviews(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Database = Depends(get_db),
) -> dict:
    limit = clamp_limit(limit, 200)
    offset = clamp_offset(offset)
    items, total = q.list_timesheet_reviews(db, status=status, limit=limit, offset=offset)
    return {
        "items": [TimesheetReviewSummary.model_validate(i) for i in items],
        "total": total,
    }


@router.get("/timesheet-review/{run_id}", response_model=TimesheetReviewStatus)
def get_timesheet_review(run_id: int, db: Database = Depends(get_db)) -> dict:
    row = writers.get_timesheet_run(db, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Timesheet review not found")
    return row
