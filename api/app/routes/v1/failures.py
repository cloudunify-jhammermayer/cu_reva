from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import get_db
from app.pagination import clamp_limit
from app.queries import reviews as q
from app.schemas.reviews import FailurePage, ReviewDetail
from reva.db.engine import Database

router = APIRouter()


@router.get("/failures", response_model=FailurePage)
def list_failures(limit: int = 20, db: Database = Depends(get_db)) -> dict:
    limit = clamp_limit(limit, 100)
    items, total = q.list_failures(db, limit=limit)
    return {"items": [ReviewDetail.model_validate(r) for r in items], "total": total}
