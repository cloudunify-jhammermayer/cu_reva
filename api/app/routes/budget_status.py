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
_HOWTO = Path(__file__).resolve().parent.parent / "static" / "how-it-works.html"
_BACKUPS = Path(__file__).resolve().parent.parent / "static" / "backups.html"
_CSS = Path(__file__).resolve().parent.parent / "static" / "reva.css"


@router.get("/reva.css", include_in_schema=False)
def shared_css() -> FileResponse:
    """The shared look of every consultant page (generated from docs-ui/src/reva.css)."""
    return FileResponse(_CSS, media_type="text/css; charset=utf-8",
                        headers={"Cache-Control": "public, max-age=300"})


@router.get("/", include_in_schema=False)
def page() -> FileResponse:
    return FileResponse(_PAGE, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})


@router.get("/how-it-works", include_in_schema=False)
def how_it_works() -> FileResponse:
    """Static developer TL;DR: when REVA reviews, commands, replies, config."""
    return FileResponse(_HOWTO, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})


@router.get("/backups", include_in_schema=False)
def backups() -> FileResponse:
    """Consultant page: the Odoo.sh backup onboarding steps and what the nightly message means."""
    return FileResponse(_BACKUPS, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})


@router.get("/data")
def data(request: Request, author: str | None = None, db: Database = Depends(get_db),
         settings: Settings = Depends(get_settings)) -> dict:
    return build_status(db, getattr(request.app.state, "rq_queue", None), settings, author=author)
