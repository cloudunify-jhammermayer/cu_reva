"""Stable RQ task entry for board Status sync."""

from worker.board_status_runner import run_board_status_update as _run
from worker.task_contract import terminal_on_permanent

run_board_status_update = terminal_on_permanent(_run)

__all__ = ["run_board_status_update"]
