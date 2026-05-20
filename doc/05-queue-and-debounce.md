# 05 — Queue and Debounce

## Overview

The queue layer sits between the webhook service and the review workers. It serves two purposes:

1. **Debounce**: When a developer pushes multiple commits in quick succession, only the latest commit gets reviewed. A 10-minute delay window absorbs rapid pushes.
2. **Job distribution**: Workers consume jobs from Redis via RQ independently, supporting concurrency control and retry logic.

## Debounce Flow

```
Push 1 (sha: aaa) → upsert pending_review, scheduled_at = now + 10 min
Push 2 (sha: bbb, 3 min later) → upsert same row, sha = bbb, scheduled_at = now + 10 min
Push 3 (sha: ccc, 2 min later) → upsert same row, sha = ccc, scheduled_at = now + 10 min
... 10 minutes of silence ...
Scheduler picks up: sha = ccc → enqueue RQ job
```

Only `ccc` gets reviewed. Pushes 1 and 2 were absorbed by the debounce.

### Manual triggers bypass debounce

When a developer comments `/review` or `/deep-review`, the pending_review is upserted with `scheduled_at = now()`. The scheduler picks it up within 30 seconds (the scheduler polling interval).

## Scheduler Implementation

The scheduler is an asyncio background task inside the FastAPI container. It runs every 30 seconds.

```python
import asyncio
from datetime import datetime
from redis import Redis
from rq import Queue
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

class ReviewScheduler:
    def __init__(self, db_session_factory, redis_url: str):
        self.db_session_factory = db_session_factory
        self.redis = Redis.from_url(redis_url)
        self.queue = Queue("reviews", connection=self.redis)
        self._running = False

    async def start(self):
        self._running = True
        while self._running:
            try:
                await self._process_pending()
            except Exception as e:
                logger.error("scheduler_error", error=str(e))
            await asyncio.sleep(30)

    async def stop(self):
        self._running = False

    async def _process_pending(self):
        async with self.db_session_factory() as db:
            # Find pending reviews whose debounce window has passed
            stmt = select(PendingReview).where(
                PendingReview.consumed == False,
                PendingReview.scheduled_at <= func.now(),
            )
            result = await db.execute(stmt)
            pending = result.scalars().all()

            for p in pending:
                # Check if a review already exists for this SHA
                existing = await db.execute(
                    select(ReviewRun).where(
                        ReviewRun.repository_id == p.repository_id,
                        ReviewRun.pull_request_id == p.pull_request_id,
                        ReviewRun.head_sha == p.head_sha,
                        ReviewRun.review_mode == p.review_mode,
                    )
                )
                if existing.scalar_one_or_none():
                    # Already reviewed or in progress — mark consumed
                    p.consumed = True
                    await db.commit()
                    continue

                # Create review_job record in DB
                job_record = ReviewJob(
                    repository_id=p.repository_id,
                    pull_request_id=p.pull_request_id,
                    head_sha=p.head_sha,
                    review_mode=p.review_mode,
                    status="queued",
                )
                db.add(job_record)
                await db.flush()

                # Enqueue RQ job
                rq_job = self.queue.enqueue(
                    "worker.tasks.run_review",
                    kwargs={
                        "job_id": job_record.id,
                        "repository_id": p.repository_id,
                        "pull_request_id": p.pull_request_id,
                        "head_sha": p.head_sha,
                        "installation_id": p.installation_id,
                        "review_mode": p.review_mode,
                        "trigger_event": p.trigger_event,
                    },
                    job_timeout="15m",     # max review duration
                    retry=None,            # we handle retries ourselves
                )

                # Link RQ job ID
                job_record.rq_job_id = rq_job.id
                p.consumed = True

                await db.commit()
                logger.info("job_enqueued",
                    repo_id=p.repository_id,
                    pr_number=p.pr_number,
                    sha=p.head_sha[:8],
                    mode=p.review_mode,
                    rq_job_id=rq_job.id,
                )
```

## Redis + RQ Configuration

### Redis Container

Redis 7 with no persistence. Data in Redis is transient — the durable state is in PostgreSQL. If Redis restarts, the scheduler will re-evaluate pending_reviews and re-enqueue.

```yaml
# docker-compose.yml
redis:
  image: redis:7-alpine
  command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru
  networks:
    - reviewer-net
  restart: unless-stopped
```

### RQ Queue Configuration

Single queue named `reviews`. Workers listen on this queue.

```python
# worker/main.py
from redis import Redis
from rq import Worker, Queue

redis_conn = Redis.from_url(settings.redis_url)
queue = Queue("reviews", connection=redis_conn)

if __name__ == "__main__":
    worker = Worker([queue], connection=redis_conn, name=f"worker-{os.getpid()}")
    worker.work(
        with_scheduler=False,  # we use our own scheduler
        logging_level="INFO",
    )
```

## Job Lifecycle

```
pending_review (scheduled_at reached)
    │
    ▼
review_job created (status: queued)
    │
    ▼
RQ job enqueued
    │
    ▼
Worker picks up job (status: started)
    │
    ├──► Success → status: completed
    │
    ├──► Transient error → retry (up to 3 attempts)
    │    └──► All retries exhausted → status: failed
    │
    ├──► Permanent error → status: failed (no retry)
    │
    └──► Stale SHA detected → status: cancelled
```

## Retry Logic

Retries are handled by the worker, not by RQ's built-in retry mechanism. This gives us more control:

```python
# worker/tasks.py

MAX_ATTEMPTS = 3
RETRY_DELAYS = [60, 180, 600]  # 1 min, 3 min, 10 min

def run_review(job_id: int, **kwargs):
    job = get_job(job_id)
    job.status = "started"
    job.attempts += 1
    job.started_at = datetime.utcnow()
    job.worker_id = os.environ.get("HOSTNAME", "unknown")
    save_job(job)

    try:
        result = execute_review(**kwargs)
        job.status = "completed"
        job.completed_at = datetime.utcnow()
        save_job(job)
    except TransientError as e:
        if job.attempts < MAX_ATTEMPTS:
            delay = RETRY_DELAYS[job.attempts - 1]
            job.status = "queued"
            job.last_error = str(e)
            save_job(job)
            # Re-enqueue with delay
            queue.enqueue_in(
                timedelta(seconds=delay),
                "worker.tasks.run_review",
                kwargs={"job_id": job_id, **kwargs},
                job_timeout="15m",
            )
        else:
            job.status = "failed"
            job.last_error = str(e)
            job.completed_at = datetime.utcnow()
            save_job(job)
            send_failure_alert(job, e)
    except PermanentError as e:
        job.status = "failed"
        job.last_error = str(e)
        job.completed_at = datetime.utcnow()
        save_job(job)
        send_failure_alert(job, e)
```

## Error Classification

| Error | Type | Retry? |
|---|---|---|
| Claude API 429 (rate limit) | Transient | Yes, with backoff |
| Claude API 500/503 | Transient | Yes |
| GitHub API 500/502/503 | Transient | Yes |
| Network timeout | Transient | Yes |
| Database connection error | Transient | Yes |
| Claude API 400 (bad request) | Permanent | No |
| GitHub 404 (PR deleted/closed) | Permanent | No |
| GitHub 403 (permissions revoked) | Permanent | No |
| JSON parse error on Claude response | Permanent | No |
| Diff too large (>1000 lines) | Permanent | No (decline) |
| PR is now draft | Permanent | No (skip) |

## Concurrency Control

For the MVP with 5 repos and low PR volume, a single worker is enough. The `docker-compose.yml` allows scaling:

```bash
# Scale to 2 workers
docker compose up -d --scale worker=2
```

Each worker processes one job at a time. With 2 workers, 2 reviews can run concurrently. Since Claude API calls take 30–120 seconds, this keeps the queue moving.

If you ever need to limit per-repo concurrency (e.g., max 1 concurrent review per repo), add a Redis lock per `repo_id` before starting the Claude call. For now, this isn't needed.

## Monitoring Queue Health

The internal API exposes queue metrics:

```
GET /api/v1/metrics/queue
```

Returns:
- Jobs in queue (waiting)
- Jobs currently executing
- Jobs completed last hour
- Jobs failed last hour
- Average queue wait time
- Average job duration
