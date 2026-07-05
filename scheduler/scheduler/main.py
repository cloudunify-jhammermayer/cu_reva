"""Scheduler entry point.

Run with: python -m scheduler.main

Polls pending_reviews every `poll_interval_seconds` and enqueues due jobs into
RQ. Runs DB migrations at startup so the scheduler can boot independently of
the api container.
"""

from __future__ import annotations

import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog
from redis import Redis
from rq import Queue

from reva.db import writers
from reva.db.engine import Database, create_engine_from_url
from reva.logging import configure_logging
from scheduler.monitor import Monitor
from scheduler.poller import Poller
from scheduler.reporter import WeeklyReporter
from scheduler.settings import Settings

logger = structlog.get_logger()


def maybe_enqueue_eviction(queue, now, last_eviction, interval_s):
    """Enqueue a repo-cache eviction job if `interval_s` has elapsed (INFR-2).

    Returns the new last-eviction timestamp (unchanged if not yet due). Only the
    worker mounts /repos, so the scheduler triggers eviction via the queue rather
    than running it directly.
    """
    if last_eviction is not None and (now - last_eviction).total_seconds() < interval_s:
        return last_eviction
    queue.enqueue("worker.runner.run_repo_cache_eviction", {})
    logger.info("repo_cache_eviction_enqueued")
    return now


def maybe_distill_memories(queue, db, now, last_distill, interval_s, min_dismissals):
    """Enqueue per-repo learned-memory distills for repos with fresh dismissal
    signal, if `interval_s` has elapsed (Tier 3 feature B). Distillation runs on
    the worker (paid Claude call), so the scheduler triggers it via the queue.
    Returns the new last-distill timestamp (unchanged if not yet due)."""
    if last_distill is not None and (now - last_distill).total_seconds() < interval_s:
        return last_distill
    due = writers.repos_due_for_memory_distill(db, min_dismissals=min_dismissals)
    for repo_id in due:
        queue.enqueue("worker.memory_distill_runner.run_memory_distill", repo_id)
    if due:
        logger.info("memory_distill_enqueued", repos=len(due))
    return now


def _previous_month_bounds(now: datetime) -> tuple[datetime, datetime]:
    first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if first_this_month.month == 1:
        start = first_this_month.replace(year=first_this_month.year - 1, month=12)
    else:
        start = first_this_month.replace(month=first_this_month.month - 1)
    return start, first_this_month


def maybe_enqueue_value_report(queue, now, last_sent, day: int, hour: int):
    """Enqueue previous-month value report once per month after day/hour."""
    if now.day < day or (now.day == day and now.hour < hour):
        return last_sent
    if last_sent is not None and last_sent.year == now.year and last_sent.month == now.month:
        return last_sent
    start, end = _previous_month_bounds(now)
    queue.enqueue(
        "worker.value_report_tasks.run_value_report",
        {"period_start_iso": start.isoformat(), "period_end_iso": end.isoformat()},
    )
    logger.info("value_report_enqueued", period_start=start.date(), period_end=end.date())
    return now


def maybe_purge_ticket_text(
    db,
    now,
    last_purge,
    interval_s,
    retention_days,
    spend_retention_days: int = 400,
    ops_events_retention_days: int = 30,
):
    """Scrub raw ticket text past the retention window if a purge is due (F1/SECU-8).

    Pure DB, so the scheduler runs it directly (unlike eviction). Returns the new
    last-purge timestamp (unchanged if not yet due).
    """
    if last_purge is not None and (now - last_purge).total_seconds() < interval_s:
        return last_purge
    purged = writers.purge_old_ticket_text(db, retention_days)
    if purged:
        logger.info("ticket_text_purged", rows=purged, retention_days=retention_days)
    purged_issue_rows = writers.purge_old_ticket_issue_text(db, retention_days)
    if purged_issue_rows:
        logger.info("ticket_issue_text_purged", rows=purged_issue_rows,
                    retention_days=retention_days)
    # M14: raw webhook payloads carry PII and grow unbounded — same cadence.
    purged_events = writers.purge_old_github_events(db, retention_days)
    if purged_events:
        logger.info("github_events_purged", rows=purged_events,
                    retention_days=retention_days)
    purged_spend = writers.purge_old_claude_spend(db, spend_retention_days)
    if purged_spend:
        logger.info(
            "claude_spend_purged",
            rows=purged_spend,
            retention_days=spend_retention_days,
        )
    purged_ops = writers.purge_old_ops_events(db, ops_events_retention_days)
    if purged_ops:
        logger.info(
            "ops_events_purged",
            rows=purged_ops,
            retention_days=ops_events_retention_days,
        )
    return now


def main() -> None:
    configure_logging()
    settings = Settings.from_env()

    engine = create_engine_from_url(settings.database_url)
    db = Database(engine)
    db.migrate(settings.migrations_dir)

    redis_conn = Redis.from_url(settings.redis_url)
    queue = Queue(settings.queue_name, connection=redis_conn)

    poller = Poller(db=db, settings=settings, queue=queue)
    reporter = WeeklyReporter(
        db=db,
        queue=queue,
        report_weekday=settings.report_weekday,
        report_hour_utc=settings.report_hour_utc,
    )
    monitor = Monitor(
        queue=queue,
        webhook_url=settings.google_chat_webhook_url,
        queue_depth_alert=settings.queue_depth_alert,
        failed_jobs_alert=settings.failed_jobs_alert,
        repo_cache_disk_pct_alert=settings.repo_cache_disk_pct_alert,
        repo_cache_dir=settings.repo_cache_dir,
    )

    stop = False

    def _handle_signal(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "scheduler_starting",
        interval_s=settings.poll_interval_seconds,
        report_weekday=settings.report_weekday,
        report_hour_utc=settings.report_hour_utc,
    )

    # The worker evicts at boot too; start the timer now so the scheduler-driven
    # eviction fires one interval out rather than redundantly at startup (INFR-2).
    last_eviction = datetime.now(timezone.utc)
    # No other trigger drives retention purges, so run one shortly after startup.
    last_purge = None
    # Memory distillation: run one shortly after startup, then on its interval.
    last_distill = None
    last_value_report = None

    while not stop:
        now = datetime.now(timezone.utc)
        poll_ok = False
        try:
            count = poller.poll()
            poll_ok = True
            if count:
                logger.info("scheduler_cycle", enqueued=count)
        except Exception:
            logger.exception("scheduler_poll_error")

        try:
            reporter.check_and_send(now)
        except Exception:
            logger.exception("scheduler_reporter_error")

        try:
            monitor.check()
        except Exception:
            logger.exception("scheduler_monitor_error")

        try:
            writers.reap_stale_running_reviews(db, settings.stale_running_seconds)
        except Exception:
            logger.exception("scheduler_reaper_error")

        try:
            last_eviction = maybe_enqueue_eviction(
                queue, now, last_eviction, settings.eviction_interval_seconds
            )
        except Exception:
            logger.exception("scheduler_eviction_error")

        try:
            last_purge = maybe_purge_ticket_text(
                db, now, last_purge, settings.retention_purge_interval_seconds,
                settings.ticket_text_retention_days,
                settings.spend_retention_days,
                settings.ops_events_retention_days,
            )
        except Exception:
            logger.exception("scheduler_retention_purge_error")

        try:
            last_distill = maybe_distill_memories(
                queue, db, now, last_distill,
                settings.memory_distill_interval_seconds,
                settings.memory_distill_min_dismissals,
            )
        except Exception:
            logger.exception("scheduler_memory_distill_error")

        try:
            last_value_report = maybe_enqueue_value_report(
                queue, now, last_value_report,
                settings.value_report_day,
                settings.value_report_hour_utc,
            )
        except Exception:
            logger.exception("scheduler_value_report_error")

        # Liveness heartbeat — the container healthcheck checks its freshness.
        # Only refresh it when the poll (the DB-dependent core loop) succeeded, so
        # a scheduler whose DB connection is permanently broken goes stale and
        # fails its healthcheck instead of looking alive forever. A transient
        # blip just skips one beat, within the healthcheck's staleness tolerance.
        if poll_ok:
            try:
                Path(settings.heartbeat_path).touch()
            except OSError:
                logger.warning("scheduler_heartbeat_write_failed", path=settings.heartbeat_path)

        for _ in range(settings.poll_interval_seconds):
            if stop:
                break
            time.sleep(1)

    logger.info("scheduler_stopped")


if __name__ == "__main__":
    main()
