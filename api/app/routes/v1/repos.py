from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import get_db
from app.queries import repos as q
from app.schemas.repos import RepoPage, RepoSummary
from reva.db.engine import Database

router = APIRouter()


@router.get("/repos", response_model=RepoPage)
def list_repos(db: Database = Depends(get_db)) -> dict:
    items, total = q.list_repos(db)
    return {"items": [RepoSummary.model_validate(r) for r in items], "total": total}
