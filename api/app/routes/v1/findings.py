from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import get_db
from app.queries import reviews as q
from app.schemas.reviews import FindingPage, FindingSummary
from reva.db.engine import Database

router = APIRouter()


@router.get("/findings", response_model=FindingPage)
def list_findings(
    severity: str | None = None,
    category: str | None = None,
    repo: str | None = None,
    limit: int = 100,
    db: Database = Depends(get_db),
) -> dict:
    limit = min(limit, 500)
    severities = [s.strip() for s in severity.split(",")] if severity else None
    items, total = q.list_findings(db, severities=severities, category=category,
                                   repo=repo, limit=limit)
    return {"items": [FindingSummary.model_validate(r) for r in items], "total": total}
