"""Stable RQ task entry point for ticket analysis.

Import path used when enqueuing: "worker.ticket_tasks.run_ticket_analysis"
"""

from worker.ticket_runner import run_ticket_analysis

__all__ = ["run_ticket_analysis"]
