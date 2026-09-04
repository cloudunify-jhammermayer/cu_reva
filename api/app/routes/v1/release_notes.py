"""Release-log lookup endpoints (spec docs/superpowers/specs/archive/2026-09-04-release-log-requirements.md, R2).

POST /api/v1/release-note   — Odoo asks for a release's log page; enqueue the lookup
GET  /api/v1/release-notes  — list lookups for the TUI Releases tab
"""

from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from rq import Retry
from sqlalchemy.exc import IntegrityError

from app.dependencies import ResolvedOdooInstance, get_db, require_odoo_instance
from app.pagination import clamp_limit, clamp_offset
from app.queries import release_notes as q
from app.schemas.release_notes import (
    ReleaseNoteCreated,
    ReleaseNotePage,
    ReleaseNoteRequest,
    ReleaseNoteSummary,
)
from reva.db import writers
from reva.db.engine import Database
from reva.release_log import release_slug
from reva.types import ReleaseNoteJobParams

router = APIRouter()
create_router = APIRouter()
logger = structlog.get_logger()

# Three retries well inside Odoo's 30-minute watchdog (spec R2). The job is a
# handful of GitHub reads plus one callback, so the timeout is generous.
_RETRY = Retry(max=3, interval=[30, 120, 300])
_FAILURE_TTL = 24 * 3600
_JOB_TIMEOUT = 300

# Odoo's watchdog escalates a pending release after 30 minutes; inside that
# window a re-submit (the "Resend to REVA" button) echoes the in-flight
# note_id so the running job's delivery is still accepted. Past it the old
# row is superseded and a fresh job starts.
_STALE_PENDING = timedelta(minutes=30)


def _is_stale_pending(row: dict) -> bool:
    created_at = row["created_at"]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at < datetime.now(timezone.utc) - _STALE_PENDING


def _enqueue(request: Request, db: Database, note_id: int, params: ReleaseNoteJobParams) -> str:
    rq_queue = request.app.state.rq_queue
    try:
        job = rq_queue.enqueue(
            "worker.release_note_tasks.run_release_note",
            params.model_dump(),
            job_timeout=_JOB_TIMEOUT,
            retry=_RETRY,
            failure_ttl=_FAILURE_TTL,
        )
    except Exception as exc:
        writers.record_release_note_failed(db, note_id, f"enqueue failed: {exc}")
        logger.error("release_note_enqueue_failed", note_id=note_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job queue unavailable; try again",
        ) from exc
    writers.attach_release_note_job_id(db, note_id, job.id)
    return job.id


@create_router.post(
    "/release-note",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ReleaseNoteCreated,
)
def submit_release_note(
    body: ReleaseNoteRequest,
    request: Request,
    db: Database = Depends(get_db),
    instance: ResolvedOdooInstance = Depends(require_odoo_instance),
) -> dict:
    # No budget gate: the lookup makes no paid call.
    slug = release_slug(body.name)

    existing = writers.get_pending_release_note(db, instance.id, body.release_id)
    if existing is not None and not _is_stale_pending(existing):
        logger.info("release_note_dedup", note_id=existing["id"])
        return {"note_id": existing["id"], "job_id": existing["job_id"], "status": "pending"}
    if existing is not None:
        writers.record_release_note_failed(
            db, existing["id"], "stale pending lookup superseded by re-submit"
        )
        logger.warning("release_note_stale_superseded", note_id=existing["id"])

    try:
        note_id = writers.record_release_note_created(
            db,
            odoo_instance_id=instance.id,
            release_id=body.release_id,
            release_name=body.name,
            slug=slug,
        )
    except IntegrityError:
        existing = writers.get_pending_release_note(db, instance.id, body.release_id)
        if existing is not None:
            logger.info("release_note_dedup_race", note_id=existing["id"])
            return {"note_id": existing["id"], "job_id": existing["job_id"], "status": "pending"}
        raise

    params = ReleaseNoteJobParams(
        note_id=note_id,
        odoo_instance_id=instance.id,
        release_id=body.release_id,
        release_name=body.name,
        slug=slug,
        github_url=body.github_url,
    )
    job_id = _enqueue(request, db, note_id, params)
    logger.info("release_note_enqueued", note_id=note_id, job_id=job_id, slug=slug)
    return {"note_id": note_id, "job_id": job_id, "status": "pending"}


@router.get("/release-notes", response_model=ReleaseNotePage)
def list_release_notes(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Database = Depends(get_db),
) -> dict:
    limit = clamp_limit(limit, 200)
    offset = clamp_offset(offset)
    items, total = q.list_release_notes(db, status=status, limit=limit, offset=offset)
    return {
        "items": [ReleaseNoteSummary.model_validate(i) for i in items],
        "total": total,
    }
