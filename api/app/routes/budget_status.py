"""Consultant-facing budget page (`/reviews`, spec 2026-09-27).

Like /repo-docs this router carries no app-layer auth: a Cloudflare Access
application must gate the `/reviews` prefix at the edge (docs/setup-production.md).
Read-only. Job args are never echoed.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from app.dependencies import get_db, get_settings
from app.queries.budget_status import build_status
from app.settings import Settings
from reva.db.engine import Database

router = APIRouter()
_PAGE = Path(__file__).resolve().parent.parent / "static" / "reviews.html"


@router.get("/", include_in_schema=False)
def page() -> FileResponse:
    return FileResponse(_PAGE, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})


@router.get("/data")
def data(request: Request, db: Database = Depends(get_db),
         settings: Settings = Depends(get_settings)) -> dict:
    return build_status(db, getattr(request.app.state, "rq_queue", None), settings)
