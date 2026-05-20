"""RQ task entry point.

The actual orchestration lives in `worker.runner`; this module exists so
the function's stable import path (`worker.tasks.run_review`) is what
the api enqueues against — implementation can be reorganized without
breaking enqueued jobs in flight.

Retry policy: jobs are enqueued with `rq.Retry(max=3, interval=[30, 120, 300])`
by the api. Only `TransientError` is retried; `PermanentError` fails the job.
"""

from __future__ import annotations

from worker.runner import run_review

__all__ = ["run_review"]
