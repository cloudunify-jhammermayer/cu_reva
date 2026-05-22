"""Admin endpoints — manual triggers for background tasks."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/weekly-report")
async def trigger_weekly_report(
    request: Request,
    days: int = 7,
) -> dict:
    """Manually enqueue a weekly report for the last `days` days.

    This does NOT record an entry in `weekly_reports`, so it won't delay
    the next scheduled send. Useful for testing the report format.
    """
    rq_queue = request.app.state.rq_queue
    job = rq_queue.enqueue("worker.runner.run_weekly_report", {"since_days": days})
    return {"status": "queued", "job_id": job.id, "since_days": days}
