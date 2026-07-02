"""RQ task entry point.

The actual orchestration lives in `worker.runner`; this module exists so
the function's stable import path (`worker.tasks.run_review`) is what
the api enqueues against — implementation can be reorganized without
breaking enqueued jobs in flight.

Retry policy: jobs are enqueued with `rq.Retry(max=3, interval=[30, 120, 300])`
by the api. Only `TransientError` is retried; `PermanentError` fails the job.
"""

from __future__ import annotations

from worker.reply_runner import run_comment_reply as _run_comment_reply
from worker.runner import run_review as _run_review
from worker.task_contract import terminal_on_permanent

__all__ = ["run_review", "run_comment_reply"]

# RQ task boundary: run a review, letting RQ retry only transient failures.
# worker.runner.run_review records the failed run, posts a failure Check Run, and
# alerts before raising PermanentError; the contract wrapper turns that into a
# terminal result so RQ (blind to exception type) doesn't re-run the doomed work.
run_review = terminal_on_permanent(_run_review)

# Same contract for inline-comment replies (M9): the reply enqueue now carries a
# retry, so a transient chat() blip retries with backoff, but a PermanentError
# (e.g. missing param) ends the job instead of RQ re-running the doomed reply.
run_comment_reply = terminal_on_permanent(_run_comment_reply)
