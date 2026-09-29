"""GET /api/v1/change-notes — read-only list of drafted change notes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import get_db
from app.pagination import clamp_limit, clamp_offset
from app.queries import change_notes as q
from app.schemas.change_notes import ChangeNotePage, ChangeNoteSummary
from reva.db.engine import Database

router = APIRouter()


@router.get("/change-notes", response_model=ChangeNotePage)
def list_change_notes(
    limit: int = 50,
    offset: int = 0,
    db: Database = Depends(get_db),
) -> dict:
    """Return a paginated list of change notes (newest first)."""
    limit = clamp_limit(limit, 200)
    offset = clamp_offset(offset)
    items, total = q.list_change_notes(db, limit=limit, offset=offset)
    return {
        "items": [ChangeNoteSummary.model_validate(i) for i in items],
        "total": total,
    }
