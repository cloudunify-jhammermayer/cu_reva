"""Monthly value-report read endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_db
from app.pagination import clamp_limit
from app.schemas.value_reports import ValueReportEntry, ValueReportPage
from reva.db import writers
from reva.db.engine import Database

router = APIRouter()


@router.get("/value-reports", response_model=ValueReportPage)
def list_value_reports(limit: int = 12, db: Database = Depends(get_db)) -> dict:
    limit = clamp_limit(limit, 24)
    items = writers.get_value_reports(db, limit=limit)
    return {"items": [ValueReportEntry.model_validate(i) for i in items], "total": len(items)}


@router.get("/value-reports/latest", response_model=ValueReportEntry)
def latest_value_report(db: Database = Depends(get_db)) -> dict:
    rows = writers.get_value_reports(db, limit=1)
    if not rows:
        raise HTTPException(status_code=404, detail="No value reports found")
    return rows[0]
