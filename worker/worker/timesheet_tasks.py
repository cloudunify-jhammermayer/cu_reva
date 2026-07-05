"""Stable RQ task entry point for timesheet wording review."""

from worker.task_contract import terminal_on_permanent
from worker.timesheet_runner import run_timesheet_review as _run_timesheet_review

run_timesheet_review = terminal_on_permanent(_run_timesheet_review)

__all__ = ["run_timesheet_review"]
