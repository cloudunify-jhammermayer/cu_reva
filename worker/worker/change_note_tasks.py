"""Stable RQ task entry for merge change notes."""

from worker.change_note_runner import run_change_note as _run_change_note
from worker.change_note_runner import run_change_note_delivery as _run_change_note_delivery
from worker.task_contract import terminal_on_permanent

run_change_note = terminal_on_permanent(_run_change_note)
deliver_change_notes = terminal_on_permanent(_run_change_note_delivery)

__all__ = ["run_change_note", "deliver_change_notes"]
