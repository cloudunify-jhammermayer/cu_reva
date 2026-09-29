"""Tests for the stuck-pending change-note reaper (spec 2026-09-30, section 5).

SQLite in-memory plus a fake queue, same pattern as test_budget_requeue.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import ChangeNote, OpsEvent
from scheduler.main import reap_change_notes

_MODEL = "helpdesk.ticket"
_STALE = 500


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


class FakeQueue:
    def __init__(self, fail_for_ticket: int | None = None):
        self.jobs: list[tuple[str, dict, dict]] = []
        self.fail_for_ticket = fail_for_ticket

    def enqueue(self, func, params, **kwargs):
        if params["ticket_id"] == self.fail_for_ticket:
            raise RuntimeError("redis down")
        self.jobs.append((func, params, kwargs))


def _pending(db, *, pr_number, ticket_id, age_seconds):
    note_id, _ = writers.get_or_create_change_note(
        db, "acme/widgets", pr_number, ticket_id, 1, _MODEL
    )
    with db.session() as s:
        s.get(ChangeNote, note_id).created_at = (
            datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        )
    return note_id


def _events(db, event):
    with db.session() as s:
        return list(s.query(OpsEvent).filter_by(component="change_note", event=event).all())


def test_stale_notes_are_reaped_and_one_job_per_ticket_is_enqueued(db):
    _pending(db, pr_number=1, ticket_id=10, age_seconds=1000)
    _pending(db, pr_number=2, ticket_id=10, age_seconds=1000)
    _pending(db, pr_number=3, ticket_id=11, age_seconds=1000)
    queue = FakeQueue()

    assert reap_change_notes(db, queue, _STALE) == 3

    assert len(_events(db, "stale_pending_reaped")) == 3
    assert sorted(
        (func, params["ticket_id"]) for func, params, _ in queue.jobs
    ) == [("worker.change_note_tasks.deliver_change_notes", 10),
          ("worker.change_note_tasks.deliver_change_notes", 11)]
    assert queue.jobs[0][1] == {
        "odoo_instance_id": 1, "ticket_id": queue.jobs[0][1]["ticket_id"], "model_name": _MODEL,
    }
    assert queue.jobs[0][2]["retry"].max == 3


def test_a_fresh_pending_note_is_left_alone(db):
    _pending(db, pr_number=1, ticket_id=10, age_seconds=100)
    queue = FakeQueue()

    assert reap_change_notes(db, queue, _STALE) == 0

    assert queue.jobs == []
    assert _events(db, "stale_pending_reaped") == []


def test_an_enqueue_failure_does_not_stop_the_other_tickets(db):
    _pending(db, pr_number=1, ticket_id=10, age_seconds=1000)
    _pending(db, pr_number=2, ticket_id=11, age_seconds=1000)
    queue = FakeQueue(fail_for_ticket=10)

    assert reap_change_notes(db, queue, _STALE) == 2

    assert [params["ticket_id"] for _, params, _ in queue.jobs] == [11]
    assert len(_events(db, "reaper_enqueue_failed")) == 1


def test_stale_change_note_seconds_follows_the_budget_wait(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.delenv("REVA_BUDGET_WAIT_MAX_SECONDS", raising=False)
    monkeypatch.delenv("REVA_BUDGET_RETRY_SECONDS", raising=False)
    from scheduler.settings import Settings

    assert Settings.from_env().stale_change_note_seconds == 172800 + 3600 + 7200

    monkeypatch.setenv("REVA_BUDGET_WAIT_MAX_SECONDS", "1000")
    assert Settings.from_env().stale_change_note_seconds == 1000 + 3600 + 7200


def test_stale_change_note_seconds_without_waiting_adds_no_retry(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("REVA_BUDGET_WAIT_MAX_SECONDS", "1000")
    monkeypatch.setenv("REVA_BUDGET_RETRY_SECONDS", "0")
    from scheduler.settings import Settings

    assert Settings.from_env().stale_change_note_seconds == 1000 + 7200
