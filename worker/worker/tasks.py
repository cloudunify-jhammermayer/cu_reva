"""RQ task entry point.

The actual orchestration lives in `worker.runner`; this module exists so
the function's stable import path (`worker.tasks.run_review`) is what
the api enqueues against — implementation can be reorganized without
breaking enqueued jobs in flight.

Retry policy: jobs are enqueued with `rq.Retry(max=3, interval=[30, 120, 300])`
by the api. Only `TransientError` is retried; `PermanentError` fails the job.
"""

from __future__ import annotations

import structlog

from reva.errors import PermanentError
from worker.runner import run_review as _run_review

logger = structlog.get_logger()

__all__ = ["run_review"]


def run_review(job_params: dict) -> dict:
    """RQ task boundary: run a review, letting RQ retry only transient failures.

    `worker.runner.run_review` already records the failed run, posts a failure
    Check Run, and sends the operator alert before raising a PermanentError.
    Re-raising it to RQ would trigger a retry — and RQ's Retry is blind to the
    exception type, so the doomed work re-runs on every attempt and re-sends an
    identical alert (e.g. a force-pushed-away SHA whose tree can never check
    out). Swallow it and return a terminal result so the job ends after one
    attempt; TransientError still propagates for RQ to retry with backoff.
    """
    try:
        return _run_review(job_params)
    except PermanentError as exc:
        logger.info("review_permanent_error_not_retried", error=str(exc))
        return {"status": "failed", "error_class": "permanent", "error": str(exc)}
