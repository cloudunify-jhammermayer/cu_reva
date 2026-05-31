from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text

from app.dependencies import get_db, get_redis
from reva.db.engine import Database

router = APIRouter()


def _ok(check) -> bool:
    try:
        check()
        return True
    except Exception:
        return False


@router.get("/health")
def health(response: Response, db: Database = Depends(get_db), redis=Depends(get_redis)) -> dict:
    """Readiness probe. 503 if any critical dependency (DB or the Redis broker)
    is unreachable, so orchestration and the TUI see a degraded API."""
    def _db_check() -> None:
        with db.session() as s:
            s.execute(text("SELECT 1"))

    db_ok = _ok(_db_check)
    redis_ok = _ok(redis.ping)
    healthy = db_ok and redis_ok
    response.status_code = 200 if healthy else 503
    return {"status": "ok" if healthy else "degraded", "db": db_ok, "redis": redis_ok}
