"""RQ task entry point for repo audit jobs."""

from __future__ import annotations

import structlog

from reva.db import writers
from reva.errors import TransientError
from reva.types import AuditJobParams
from worker.runner import budget_exceeded, get_context

logger = structlog.get_logger()


def run_audit(job_params: dict) -> dict:
    """RQ task: run a full repo audit and persist the result."""
    from reva.db.models import AuditRun
    from sqlalchemy import insert, update

    ctx = get_context()
    params = AuditJobParams.model_validate(job_params)
    log = logger.bind(repository_id=params.repository_id)
    log.info("audit_job_start")

    # SECU-4: the audit is the most expensive Claude path — respect the rolling
    # cap. Decline a NEW audit when over budget (no row created); in-flight
    # audits are never interrupted.
    spent = budget_exceeded(ctx)
    if spent is not None:
        log.warning("audit_over_budget", spent_usd=round(spent, 2),
                    budget_usd=ctx.daily_budget_usd)
        return {"audit_id": None, "status": "declined", "reason": "over_budget"}

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
    except TransientError:
        # Don't mark as failed — RQ will retry and create a new run row.
        log.warning("audit_job_transient_error", exc_info=True)
        raise
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

    # CORR-11/SECU-4: record audit spend in the unified ledger so the cap counts it.
    writers.record_claude_spend(ctx.db, "audit", result.estimated_cost_usd)

    log.info("audit_job_done", findings=len(result.findings),
             cost_usd=result.estimated_cost_usd)
    return {"audit_id": audit_id, "status": "completed", "findings": len(result.findings)}
