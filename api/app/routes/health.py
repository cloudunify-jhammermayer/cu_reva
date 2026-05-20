from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.dependencies import get_db
from reva.db.engine import Database

router = APIRouter()


@router.get("/health")
def health(db: Database = Depends(get_db)) -> dict:
    try:
        with db.session() as s:
            s.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    status = "ok" if db_ok else "degraded"
    return {"status": status, "db": db_ok}
