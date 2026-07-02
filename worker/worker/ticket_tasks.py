"""Stable RQ task entry point for ticket analysis.

Import path used when enqueuing: "worker.ticket_tasks.run_ticket_analysis"

The job is enqueued with retry=, so it goes through the shared task contract: a
PermanentError ends the job terminally instead of RQ re-running (and re-paying
for) a doomed analysis; TransientError still retries with backoff (M4).
"""

from worker.task_contract import terminal_on_permanent
from worker.ticket_runner import run_ticket_analysis as _run_ticket_analysis

run_ticket_analysis = terminal_on_permanent(_run_ticket_analysis)

__all__ = ["run_ticket_analysis"]
