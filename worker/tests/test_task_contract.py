"""Tests for the shared RQ task-boundary contract (M4/M5).

RQ Retry is blind to the exception type, so every retried task entry point must
turn a PermanentError into a terminal result (no retry) while letting
TransientError propagate for RQ to retry.
"""

from __future__ import annotations

import pytest

from reva.errors import PermanentError, TransientError
from worker.task_contract import terminal_on_permanent


def test_permanent_error_becomes_terminal_result():
    @terminal_on_permanent
    def task(_params):
        raise PermanentError("force-pushed-away SHA")

    out = task({"x": 1})
    assert out == {
        "status": "failed",
        "error_class": "permanent",
        "error": "force-pushed-away SHA",
    }


def test_transient_error_propagates_for_retry():
    @terminal_on_permanent
    def task(_params):
        raise TransientError("rate limited")

    with pytest.raises(TransientError):
        task({})


def test_success_result_passes_through():
    @terminal_on_permanent
    def task(_params):
        return {"status": "completed", "id": 7}

    assert task({}) == {"status": "completed", "id": 7}


def test_all_retried_entry_points_are_wrapped():
    """Every retried RQ entry point (M4/M9) must go through the contract; a bare
    re-export would let RQ retry a PermanentError."""
    from worker import tasks, ticket_issue_tasks, ticket_tasks

    for fn in (
        tasks.run_review,
        tasks.run_comment_reply,
        ticket_tasks.run_ticket_analysis,
        ticket_issue_tasks.run_ticket_issues,
        ticket_issue_tasks.sync_ticket_issue_state,
    ):
        assert getattr(fn, "__wrapped__", None) is not None, f"{fn} not wrapped"
