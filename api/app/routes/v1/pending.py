from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import get_db
from app.queries import reviews as q
from app.schemas.reviews import PendingPage, PendingReview
from reva.db.engine import Database

router = APIRouter()


@router.get("/pending", response_model=PendingPage)
def list_pending(db: Database = Depends(get_db)) -> dict:
    items, total = q.list_pending(db)
    return {"items": [PendingReview.model_validate(r) for r in items], "total": total}
