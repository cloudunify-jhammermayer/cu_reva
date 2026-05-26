"""RQ task entry point for repo audit jobs."""

from __future__ import annotations

import structlog

from reva.types import AuditJobParams
from worker.runner import get_context

logger = structlog.get_logger()


def run_audit(job_params: dict) -> dict:
    """RQ task: run a full repo audit and persist the result."""
    from reva.db.models import AuditRun
    from sqlalchemy import insert, update

    ctx = get_context()
    params = AuditJobParams.model_validate(job_params)
    log = logger.bind(repository_id=params.repository_id)
    log.info("audit_job_start")

    with ctx.db.session() as s:
        result_row = s.execute(
            insert(AuditRun).values(
                repository_id=params.repository_id,
                status="started",
                requested_by=params.requested_by,
            ).returning(AuditRun.id)
        )
        audit_id = result_row.scalar_one()
        s.commit()

    try:
        result = ctx.auditor.execute(params)
    except Exception as exc:
        with ctx.db.session() as s:
            s.execute(
                update(AuditRun)
                .where(AuditRun.id == audit_id)
                .values(status="failed", error_message=str(exc)[:500])
            )
            s.commit()
        log.error("audit_job_failed", error=str(exc))
        raise

    with ctx.db.session() as s:
        s.execute(
            update(AuditRun)
            .where(AuditRun.id == audit_id)
            .values(
                status="completed",
                summary=result.summary,
                model=result.model,
                started_at=result.started_at,
                completed_at=result.completed_at,
                duration_ms=result.duration_ms,
                finding_count=len(result.findings),
            )
        )
        s.commit()

    log.info("audit_job_done", findings=len(result.findings))
    return {"audit_id": audit_id, "status": "completed", "findings": len(result.findings)}
