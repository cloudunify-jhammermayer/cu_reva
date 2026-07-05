"""Stable RQ task entry for monthly value reports."""

from worker.task_contract import terminal_on_permanent
from worker.value_report_runner import run_value_report as _run_value_report

run_value_report = terminal_on_permanent(_run_value_report)

__all__ = ["run_value_report"]
