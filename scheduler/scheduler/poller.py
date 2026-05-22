"""Poll pending_reviews and enqueue due jobs into RQ.

One `poll()` call per scheduler tick. Each due pending_review is consumed
exactly once: `consumed` is set to True inside the same transaction that reads
it, so a crashed scheduler never double-enqueues on restart.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from redis import Redis
from rq import Queue, Retry
from sqlalchemy import select

from reva.db.engine import Database
from reva.db.models import PendingReview, ReviewRun
from scheduler.settings import Settings

logger = structlog.get_logger()

# Matches rq.Retry config locked in HANDOFF.md.
_RETRY = Retry(max=3, interval=[30, 120, 300])
_JOB_TIMEOUT = 900  # 15 minutes


class Poller:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        queue: Queue | None = None,
    ) -> None:
        self._db = db
        if queue is not None:
            self._queue = queue
        else:
            redis = Redis.from_url(settings.redis_url)
            self._queue = Queue(settings.queue_name, connection=redis)

    def poll(self) -> int:
        """Enqueue all due pending reviews. Returns the count of jobs enqueued."""
        enqueued = 0
        now = datetime.now(timezone.utc)

        # Fetch IDs only — each review is then processed in its own transaction
        # so a Redis failure for one job doesn't roll back the others.
        with self._db.session() as s:
            pending_ids = s.execute(
                select(PendingReview.id).where(
                    PendingReview.consumed == False,  # noqa: E712
                    PendingReview.scheduled_at <= now,
                )
            ).scalars().all()

        for pending_id in pending_ids:
            if self._consume_and_enqueue(pending_id):
                enqueued += 1

        return enqueued

    def _consume_and_enqueue(self, pending_id: int) -> bool:
        """Process one pending review in its own DB transaction.

        Marks consumed=True and enqueues inside the same transaction so a
        Redis failure for this job rolls back only this row (not a whole batch).
        Re-fetches by primary key to guard against concurrent consumption.
        Returns True if a job was enqueued.
        """
        with self._db.session() as s:
            pending = s.get(PendingReview, pending_id)
            if pending is None or pending.consumed:
                return False

            # Idempotency: skip if this exact (sha, mode) was already reviewed,
            # unless this is an explicit manual requeue.
            if pending.trigger_event not in ("manual_requeue", "comment"):
                already_exists = s.execute(
                    select(ReviewRun).where(
                        ReviewRun.repository_id == pending.repository_id,
                        ReviewRun.pull_request_id == pending.pull_request_id,
                        ReviewRun.head_sha == pending.head_sha,
                        ReviewRun.review_mode == pending.review_mode,
                    )
                ).scalar_one_or_none()

                if already_exists is not None:
                    logger.info(
                        "scheduler_skip_already_reviewed",
                        repo_id=pending.repository_id,
                        pr=pending.pr_number,
                        sha=pending.head_sha[:8],
                    )
                    pending.consumed = True
                    return False

            pending.consumed = True
            job_params = {
                "repository_id": pending.repository_id,
                "pull_request_id": pending.pull_request_id,
                "head_sha": pending.head_sha,
                "installation_id": pending.installation_id,
                "review_mode": pending.review_mode,
                "trigger_event": pending.trigger_event,
            }
            self._queue.enqueue(
                "worker.tasks.run_review",
                job_params,
                job_timeout=_JOB_TIMEOUT,
                retry=_RETRY,
            )
            logger.info(
                "scheduler_enqueued",
                repo_id=pending.repository_id,
                pr=pending.pr_number,
                sha=pending.head_sha[:8],
                mode=pending.review_mode,
            )
        return True
