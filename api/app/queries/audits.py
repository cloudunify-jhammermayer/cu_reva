"""Read queries for audit_findings."""

from __future__ import annotations

from sqlalchemy import case, func, select

from reva.db.engine import Database
from reva.db.models import AuditFinding, AuditRun, Repository

# Severity sort order (critical first).
_SEVERITY_ORDER = case(
    (AuditFinding.severity == "critical", 0),
    (AuditFinding.severity == "major", 1),
    (AuditFinding.severity == "minor", 2),
    else_=3,
)


def list_audit_runs(db: Database, *, limit: int = 50) -> tuple[list[dict], int]:
    """Audit RUNS newest-first, with repo name + how many findings became issues.

    This is the run-status feed (running / completed / failed) for the TUI."""
    issued = (
        select(func.count(AuditFinding.id))
        .where(AuditFinding.audit_run_id == AuditRun.id)
        .where(AuditFinding.github_issue_number.isnot(None))
        .correlate(AuditRun)
        .scalar_subquery()
    )
    with db.session() as s:
        total = s.execute(select(func.count()).select_from(AuditRun)).scalar_one()
        rows = s.execute(
            select(AuditRun, Repository.full_name, issued.label("issued_count"))
            .join(Repository, AuditRun.repository_id == Repository.id)
            .order_by(AuditRun.id.desc())
            .limit(limit)
        ).all()
        items = [
            {
                "id": r.id,
                "repo_full_name": full_name,
                "status": r.status,
                "model": r.model,
                "finding_count": r.finding_count,
                "issued_count": issued_count,
                "duration_ms": r.duration_ms,
                "requested_by": r.requested_by,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
            }
            for r, full_name, issued_count in rows
        ]
    return items, total


def list_audit_findings(
    db: Database,
    *,
    severities: list[str] | None = None,
    repo: str | None = None,
    audit_run_id: int | None = None,
    limit: int = 100,
) -> tuple[list[dict], int]:
    """Audit findings across repos, newest/most-severe first, with repo name."""
    with db.session() as s:
        base = (
            select(AuditFinding, Repository.full_name)
            .join(AuditRun, AuditFinding.audit_run_id == AuditRun.id)
            .join(Repository, AuditRun.repository_id == Repository.id)
        )
        if severities:
            base = base.where(AuditFinding.severity.in_(severities))
        if repo:
            base = base.where(Repository.full_name == repo)
        if audit_run_id is not None:
            base = base.where(AuditFinding.audit_run_id == audit_run_id)

        total = s.execute(select(func.count()).select_from(base.subquery())).scalar_one()
        rows = s.execute(
            base.order_by(_SEVERITY_ORDER, AuditFinding.id.desc()).limit(limit)
        ).all()

        items = [
            {
                "id": f.id,
                "audit_run_id": f.audit_run_id,
                "repo_full_name": full_name,
                "severity": f.severity,
                "category": f.category,
                "title": f.title,
                "confidence": float(f.confidence) if f.confidence is not None else None,
                "file_path": f.file_path,
                "line_start": f.line_start,
                "github_issue_number": f.github_issue_number,
                "created_at": f.created_at,
            }
            for f, full_name in rows
        ]
    return items, total
