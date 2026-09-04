"""Stable RQ task entry point for the release-log lookup.

Import path used when enqueuing: "worker.release_note_tasks.run_release_note".
Enqueued with retry=, so it goes through the shared task contract: a
PermanentError ends the job terminally instead of RQ re-running it (and
re-firing the failed Odoo callback); TransientError still retries.
"""

from worker.release_note_runner import run_release_note as _run_release_note
from worker.task_contract import terminal_on_permanent

run_release_note = terminal_on_permanent(_run_release_note)

__all__ = ["run_release_note"]
