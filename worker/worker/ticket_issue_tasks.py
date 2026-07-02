"""Stable RQ task entry points for ticket issue creation and state sync.

Import paths used when enqueuing:
    "worker.ticket_issue_tasks.run_ticket_issues"
    "worker.ticket_issue_tasks.sync_ticket_issue_state"

Both are enqueued with retry=, so they go through the shared task contract: a
PermanentError ends the job terminally instead of RQ re-running it (and re-firing
the failed Odoo callback) on every attempt; TransientError still retries (M4).
"""

from worker.task_contract import terminal_on_permanent
from worker.ticket_issue_runner import (
    run_ticket_issues as _run_ticket_issues,
    sync_ticket_issue_state as _sync_ticket_issue_state,
)

run_ticket_issues = terminal_on_permanent(_run_ticket_issues)
sync_ticket_issue_state = terminal_on_permanent(_sync_ticket_issue_state)

__all__ = ["run_ticket_issues", "sync_ticket_issue_state"]
