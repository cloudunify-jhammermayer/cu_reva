"""RQ worker bootstrap.

Run with: `python -m worker.main`

Loads `Settings` from environment, builds the singleton `WorkerContext`
(which runs DB migrations as a side effect), then hands control to RQ.
"""

from __future__ import annotations

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
    worker = Worker([queue], connection=connection)

    logger.info(
        "worker_starting",
        queue=settings.queue_name,
        redis_url=settings.redis_url,
    )
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
