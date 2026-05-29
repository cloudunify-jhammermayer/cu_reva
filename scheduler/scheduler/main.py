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

import structlog
from redis import Redis
from rq import Queue

from reva.db.engine import Database, create_engine_from_url
from scheduler.monitor import Monitor
from scheduler.poller import Poller
from scheduler.reporter import WeeklyReporter
from scheduler.settings import Settings

logger = structlog.get_logger()


def main() -> None:
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

    while not stop:
        now = datetime.now(timezone.utc)
        try:
            count = poller.poll()
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

        for _ in range(settings.poll_interval_seconds):
            if stop:
                break
            time.sleep(1)

    logger.info("scheduler_stopped")


if __name__ == "__main__":
    main()
