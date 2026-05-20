"""Scheduler entry point.

Run with: python -m scheduler.main

Polls pending_reviews every `poll_interval_seconds` and enqueues due jobs into
RQ. Runs DB migrations at startup so the scheduler can boot independently of
the api container.
"""

from __future__ import annotations

import signal
import time

import structlog

from reva.db.engine import Database, create_engine_from_url
from scheduler.poller import Poller
from scheduler.settings import Settings

logger = structlog.get_logger()


def main() -> None:
    settings = Settings.from_env()

    engine = create_engine_from_url(settings.database_url)
    db = Database(engine)
    db.migrate(settings.migrations_dir)

    poller = Poller(db=db, settings=settings)

    stop = False

    def _handle_signal(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("scheduler_starting", interval_s=settings.poll_interval_seconds)

    while not stop:
        try:
            count = poller.poll()
            if count:
                logger.info("scheduler_cycle", enqueued=count)
        except Exception:
            logger.exception("scheduler_poll_error")

        for _ in range(settings.poll_interval_seconds):
            if stop:
                break
            time.sleep(1)

    logger.info("scheduler_stopped")


if __name__ == "__main__":
    main()
