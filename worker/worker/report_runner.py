"""Weekly stats report job.

Builds and posts the weekly stats digest to Google Chat. Split out of
worker.runner so the report path is its own module; not RQ-retried (a missed
report is logged, never retried), so there is no task-contract wrapper.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from reva.notifications import post_to_chat
from reva.weekly_report import build_weekly_report
from worker.runner import get_context

logger = structlog.get_logger()


def run_weekly_report(params: dict | None = None) -> None:
    """RQ task: build and post the weekly stats report to Google Chat.

    params (all optional):
      since_days  int   look-back window in days (default 7)
    """
    ctx = get_context()
    if not ctx.google_chat_webhook_url:
        logger.info("weekly_report_skipped_no_webhook")
        return

    since_days = int((params or {}).get("since_days", 7))
    since = datetime.now(timezone.utc) - timedelta(days=since_days)

    try:
        message = build_weekly_report(ctx.db, since=since)
    except Exception:
        logger.exception("weekly_report_build_failed")
        return

    # SECU-15: go through post_to_chat so the webhook host is SSRF-validated like
    # the alert path (was a raw httpx.post that skipped the check).
    if post_to_chat(ctx.google_chat_webhook_url, message, timeout=10):
        logger.info("weekly_report_sent", since_days=since_days)
    else:
        logger.warning("weekly_report_send_failed")
