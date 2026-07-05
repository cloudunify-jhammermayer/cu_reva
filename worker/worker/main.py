"""RQ worker bootstrap.

Run with: `python -m worker.main`

Loads `Settings` from environment, builds the singleton `WorkerContext`
(which runs DB migrations as a side effect), then hands control to RQ.
"""

from __future__ import annotations

import socket

import structlog
from redis import Redis
from rq import Queue, Worker

from reva.logging import configure_logging
from worker.runner import build_worker_context
from worker.settings import Settings

logger = structlog.get_logger()


def main() -> None:
    configure_logging()
    settings = Settings.from_env()

    build_worker_context(settings)

    connection = Redis.from_url(settings.redis_url)
    queue = Queue(settings.queue_name, connection=connection)
    worker = Worker([queue], connection=connection, name=socket.gethostname())

    logger.info(
        "worker_starting",
        queue=settings.queue_name,
        redis_url=settings.redis_url,
    )
    # with_scheduler: rq.Retry(interval=...) parks failed jobs in the
    # ScheduledJobRegistry; only a scheduler-enabled worker moves them back to
    # the queue when due. Without it every retry (reviews via the poller,
    # create-issues callbacks, issue-state syncs) is scheduled once and then
    # stranded forever. Safe with multiple workers (lock-guarded).
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
