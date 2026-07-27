"""Stable RQ task entry point for support answers.

Import path used when enqueuing: "worker.support_tasks.run_support_answer"

The job is enqueued with retry=, so it goes through the shared task contract: a
PermanentError ends the job terminally instead of RQ re-running (and re-paying
for) a doomed turn; TransientError still retries with backoff — which is what a
busy repo lock on the code-grounded path relies on.
"""

from worker.support_runner import run_support_answer as _run_support_answer
from worker.task_contract import terminal_on_permanent

run_support_answer = terminal_on_permanent(_run_support_answer)

__all__ = ["run_support_answer"]
