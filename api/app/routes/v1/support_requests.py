"""Support-answer endpoints.

POST /api/v1/support-request            — ask REVA a question (fire-and-forget)
GET  /api/v1/support-turn/{turn_id}     — poll for status / result
POST /api/v1/support-turn/{turn_id}/requeue
GET  /api/v1/support-threads            — dashboard list (master key)
GET  /api/v1/support-threads/{thread_id} — thread + its turns (drill-down)

Mirrors the ticket-analysis endpoints: instance-key gate on create, 202 +
dedup, RQ enqueue with retry, and a timeout derived from REVIEW_JOB_TIMEOUT
because a turn can escalate to a headless-CLI run against a repo clone.
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
    require_master_or_odoo_instance,
    require_odoo_instance,
)
from app.pagination import clamp_limit, clamp_offset
from app.schemas.support_requests import (
    SupportRequestBody,
    SupportRequestCreated,
    SupportThreadDetail,
    SupportThreadPage,
    SupportTurnStatus,
)
from reva.attachment_text import classify_attachment
from reva.claude_code_runner import REVIEW_JOB_TIMEOUT
from reva.db import writers
from reva.db.engine import Database
from reva.github_urls import parse_github_repo_url
from reva.image_attachment import (
    MAX_IMAGES,
    MAX_TOTAL_IMAGE_BYTES,
    classify_image,
)
from reva.types import ImageAttachment, SupportJobParams

router = APIRouter()
create_router = APIRouter()  # instance-key gated
shared_router = APIRouter()  # master OR instance key; instance sees only its rows
logger = structlog.get_logger()

# A turn can escalate to a CLI run against a clone (planner-gated code
# grounding), so it needs the review-class timeout — at 300s RQ would SIGKILL
# the work-horse mid-paid-run and _RETRY would re-pay twice more.
_JOB_TIMEOUT = REVIEW_JOB_TIMEOUT
_RETRY = Retry(max=3, interval=[30, 120, 300])
_FAILURE_TTL = 24 * 3600
# Must outlive the worst case a job can still be legitimately running, or a
# stale-requeue starts a second paid run beside a live one.
_STALE_PENDING = timedelta(
    seconds=(_RETRY.max + 1) * _JOB_TIMEOUT + sum(_RETRY.intervals)
) + timedelta(minutes=15)


def _enqueue(request: Request, db: Database, turn_id: int, params: SupportJobParams) -> str:
    rq_queue = request.app.state.rq_queue
    try:
        job = rq_queue.enqueue(
            "worker.support_tasks.run_support_answer",
            params.model_dump(mode="json"),
            job_timeout=_JOB_TIMEOUT,
            retry=_RETRY,
            failure_ttl=_FAILURE_TTL,
        )
    except Exception as exc:
        # Mark it failed so the pending dedup doesn't pin future submits to a
        # turn no worker will ever process.
        writers.record_support_turn_failed(db, turn_id, f"enqueue failed: {exc}")
        logger.error("support_request_enqueue_failed", turn_id=turn_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job queue unavailable; try again",
        ) from exc
    writers.attach_support_job_id(db, turn_id, job.id)
    return job.id


def _assert_images_acceptable(images: list[ImageAttachment]) -> None:
    """Accept-time image gate: count, per-image type/size, label shape, total
    budget, and label uniqueness. 422 is the only error channel back to Odoo,
    so every rejection names the offending image."""
    if len(images) > MAX_IMAGES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"images: at most {MAX_IMAGES} images per request, got {len(images)}",
        )
    seen_labels: set[str] = set()
    total = 0
    for image in images:
        try:
            _, data = classify_image(image.filename, image.label, image.content_base64)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"images: {exc}",
            ) from exc
        # Duplicate labels would make two blocks indistinguishable to the model
        # AND ambiguous against the [Image N] markers in the question text.
        if image.label in seen_labels:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"images: duplicate label {image.label!r}",
            )
        seen_labels.add(image.label)
        total += len(data)
        if total > MAX_TOTAL_IMAGE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"images: total decoded size exceeds {MAX_TOTAL_IMAGE_BYTES} bytes"
                ),
            )


def _is_stale_pending(row: dict) -> bool:
    created_at = row["created_at"]
    if created_at.tzinfo is None:  # SQLite returns naive datetimes
        created_at = created_at.replace(tzinfo=timezone.utc)
    return row["status"] == "pending" and created_at < datetime.now(timezone.utc) - _STALE_PENDING


@create_router.post(
    "/support-request",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SupportRequestCreated,
)
def submit_support_request(
    body: SupportRequestBody,
    request: Request,
    db: Database = Depends(get_db),
    instance: ResolvedOdooInstance = Depends(require_odoo_instance),
) -> dict:
    """Accept a support question, enqueue the answer job, return immediately."""
    assert_instance_within_budget(db, instance)
    if body.attachment is not None:
        try:
            classify_attachment(body.attachment.filename, body.attachment.content_base64)
        except ValueError as exc:
            # Accept-time 422 is the only error channel back to Odoo; worker
            # failures only land in the DB.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"attachment: {exc}",
            ) from exc
    _assert_images_acceptable(body.images)
    if body.github_url is not None and parse_github_repo_url(body.github_url) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="github_url must be an https://github.com/{owner}/{repo} URL",
        )

    thread_id = writers.get_or_create_support_thread(
        db,
        odoo_instance_id=instance.id,
        ticket_id=body.ticket_id,
        model_name=body.model_name,
        field_name=body.field_name,
        github_url=body.github_url,
    )

    existing = writers.get_pending_support_turn(db, thread_id)
    if existing is not None:
        logger.info("support_request_dedup", turn_id=existing["id"], thread_id=thread_id)
        return {
            "thread_id": thread_id, "turn_id": existing["id"],
            "job_id": existing["job_id"], "status": "pending",
        }

    try:
        turn_id = writers.record_support_turn_created(
            db, thread_id, instance.id, body.question, image_count=len(body.images)
        )
    except IntegrityError:
        # Two concurrent POSTs raced past the dedup; the one-pending-turn index
        # picked a winner. Return it rather than creating a second paid job.
        existing = writers.get_pending_support_turn(db, thread_id)
        if existing is not None:
            logger.info("support_request_dedup_race", turn_id=existing["id"])
            return {
                "thread_id": thread_id, "turn_id": existing["id"],
                "job_id": existing["job_id"], "status": "pending",
            }
        raise

    params = SupportJobParams(
        turn_id=turn_id,
        thread_id=thread_id,
        odoo_instance_id=instance.id,
        ticket_id=body.ticket_id,
        model_name=body.model_name,
        field_name=body.field_name,
        subject=body.subject,
        question=body.question,
        github_url=body.github_url,
        persona_context=body.persona_context,
        chatter=body.chatter,
        attachment=body.attachment,
        images=body.images,
    )
    job_id = _enqueue(request, db, turn_id, params)
    logger.info("support_request_enqueued", turn_id=turn_id, job_id=job_id)
    return {"thread_id": thread_id, "turn_id": turn_id, "job_id": job_id,
            "status": "pending"}


@shared_router.get("/support-turn/{turn_id}", response_model=SupportTurnStatus)
def get_support_turn(
    turn_id: int,
    db: Database = Depends(get_db),
    instance: ResolvedOdooInstance | None = Depends(require_master_or_odoo_instance),
) -> dict:
    """Current status and result of one turn."""
    row = writers.get_support_turn(db, turn_id)
    # An instance key sees only its own rows; cross-instance ids 404 — not 403 —
    # so ids aren't probeable.
    if row is None or (instance is not None and row["odoo_instance_id"] != instance.id):
        raise HTTPException(status_code=404, detail="Support turn not found")
    return row


@shared_router.post(
    "/support-turn/{turn_id}/requeue",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SupportRequestCreated,
)
def requeue_support_turn(
    turn_id: int,
    request: Request,
    db: Database = Depends(get_db),
    instance: ResolvedOdooInstance | None = Depends(require_master_or_odoo_instance),
) -> dict:
    """Re-run a failed/completed turn, or a stale pending one whose job died."""
    row = writers.get_support_turn(db, turn_id)
    if row is None or (instance is not None and row["odoo_instance_id"] != instance.id):
        raise HTTPException(status_code=404, detail="Support turn not found")
    if row["status"] not in ("failed", "completed") and not _is_stale_pending(row):
        raise HTTPException(
            status_code=409,
            detail="Only failed, completed, or stale pending turns can be requeued",
        )
    other = writers.get_pending_support_turn(db, row["thread_id"])
    if other is not None and other["id"] != turn_id:
        raise HTTPException(
            status_code=409,
            detail=f"Turn {other['id']} is already pending on this thread",
        )

    thread = writers.get_support_thread(db, row["thread_id"])
    params = SupportJobParams(
        turn_id=turn_id,
        thread_id=row["thread_id"],
        odoo_instance_id=row["odoo_instance_id"],
        ticket_id=thread["ticket_id"],
        model_name=thread["model_name"],
        field_name=thread["field_name"],
        subject="",
        question=row["question"],
        # Replayed from the thread: without it a requeued turn silently loses
        # repo-docs grounding and can never escalate (the bug the ticket path
        # shipped with).
        github_url=thread["github_url"],
        chatter=[],
    )
    # Images are not stored (they ride in the RQ payload, like `attachment`),
    # so a requeue re-runs the turn blind. On a ticket whose screenshots ARE the
    # question that is indistinguishable from a well-grounded answer — say so
    # rather than letting it pass silently. Re-pressing the Odoo button is the
    # fix; it resends the images on a fresh turn.
    if row.get("image_count"):
        writers.record_ops_event(
            db, "support_answer", "warning", "requeue_lost_images",
            {"turn_id": turn_id, "image_count": row["image_count"]},
        )
        logger.warning("support_turn_requeue_lost_images", turn_id=turn_id,
                       image_count=row["image_count"])

    writers.reset_support_turn(db, turn_id)
    job_id = _enqueue(request, db, turn_id, params)
    logger.info("support_turn_requeued", turn_id=turn_id, job_id=job_id)
    return {"thread_id": row["thread_id"], "turn_id": turn_id, "job_id": job_id,
            "status": "pending"}


@router.get("/support-threads", response_model=SupportThreadPage)
def list_support_threads(
    limit: int = 50,
    offset: int = 0,
    db: Database = Depends(get_db),
) -> dict:
    """Paginated thread list for the dashboard."""
    limit = clamp_limit(limit, 200)
    offset = clamp_offset(offset)
    items = writers.list_support_threads(db, limit=limit)
    return {"items": items, "total": len(items)}


@router.get("/support-threads/{thread_id}", response_model=SupportThreadDetail)
def get_support_thread(thread_id: int, db: Database = Depends(get_db)) -> dict:
    """One thread with all of its turns, oldest first.

    The drill-down the dashboard needs: without it a thread row exposes no turn
    id at all, so there is no way to reach a turn except by knowing its id.
    """
    thread = writers.get_support_thread(db, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Support thread not found")
    return {**thread, "turns": writers.list_support_turns(db, thread_id)}
