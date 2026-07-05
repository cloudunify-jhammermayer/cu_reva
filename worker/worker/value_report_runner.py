"""Monthly value-report job."""

from __future__ import annotations

from datetime import datetime

import httpx
import structlog

from reva.db import writers
from reva.value_report import build_report
from worker.runner import get_context

logger = structlog.get_logger()


def notify_value_report(webhook_url: str, summary: str) -> None:
    if not webhook_url:
        return
    httpx.post(webhook_url, json={"text": summary}, timeout=10.0).raise_for_status()


def run_value_report(job_params: dict) -> dict:
    ctx = get_context()
    start = datetime.fromisoformat(job_params["period_start_iso"])
    end = datetime.fromisoformat(job_params["period_end_iso"])
    md, stats = build_report(ctx.db, start, end)
    report_id = writers.upsert_value_report(ctx.db, start, end, md, stats)
    logger.info("value_report_persisted", report_id=report_id)
    if not ctx.value_report_chat_enabled:
        return {"status": "persisted", "report_id": report_id}
    try:
        notify_value_report(ctx.google_chat_webhook_url, md[:3500])
        writers.set_value_report_chat_sent(ctx.db, report_id)
        return {"status": "sent", "report_id": report_id}
    except Exception as exc:
        writers.record_ops_event(
            ctx.db,
            "value_report",
            "warning",
            "chat_failed",
            {"error": str(exc)[:300]},
        )
        return {"status": "persisted_chat_failed", "report_id": report_id}
