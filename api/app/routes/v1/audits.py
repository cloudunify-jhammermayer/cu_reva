from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import get_db
from app.pagination import clamp_limit
from app.queries import audits as q
from app.schemas.audits import (
    AuditFindingPage,
    AuditFindingSummary,
    AuditRunPage,
    AuditRunSummary,
)
from reva.db.engine import Database

router = APIRouter()


@router.get("/audits", response_model=AuditRunPage)
def list_audit_runs(
    limit: int = 50,
    db: Database = Depends(get_db),
) -> dict:
    """Audit RUNS (status feed) — newest first."""
    items, total = q.list_audit_runs(db, limit=clamp_limit(limit, 200))
    return {"items": [AuditRunSummary.model_validate(r) for r in items], "total": total}


@router.get("/audit-findings", response_model=AuditFindingPage)
def list_audit_findings(
    severity: str | None = None,
    repo: str | None = None,
    audit_run_id: int | None = None,
    limit: int = 100,
    db: Database = Depends(get_db),
) -> dict:
    limit = clamp_limit(limit, 500)
    severities = [s.strip() for s in severity.split(",")] if severity else None
    items, total = q.list_audit_findings(
        db, severities=severities, repo=repo, audit_run_id=audit_run_id, limit=limit
    )
    return {"items": [AuditFindingSummary.model_validate(r) for r in items], "total": total}
