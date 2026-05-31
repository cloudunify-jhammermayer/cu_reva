from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_db
from app.pagination import clamp_limit, clamp_offset
from app.queries import reviews as q
from app.schemas.reviews import ReviewDetail, ReviewPage, ReviewSummary
from reva.db.engine import Database

router = APIRouter()
logger = structlog.get_logger()


@router.get("/reviews", response_model=ReviewPage)
def list_reviews(
    repo: str | None = None,
    status: str | None = None,
    author: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Database = Depends(get_db),
) -> dict:
    limit = clamp_limit(limit, 200)
    offset = clamp_offset(offset)
    statuses = [s.strip() for s in status.split(",")] if status else None
    items, total = q.list_reviews(db, repo=repo, statuses=statuses, author=author,
                                  limit=limit, offset=offset)
    return {"items": [ReviewSummary.model_validate(r) for r in items], "total": total}


@router.get("/reviews/{review_run_id}", response_model=ReviewDetail)
def get_review(review_run_id: int, db: Database = Depends(get_db)) -> ReviewDetail:
    row = q.get_review_detail(db, review_run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Review not found")
    return ReviewDetail.model_validate(row)


@router.post("/reviews/{review_run_id}/requeue", status_code=202)
def requeue_review(review_run_id: int, db: Database = Depends(get_db)) -> dict:
    ok = q.requeue_review(db, review_run_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Review not found or not in a requeable state (failed/stale/completed)")
    logger.info("review_requeued", review_run_id=review_run_id)
    return {"status": "queued"}
