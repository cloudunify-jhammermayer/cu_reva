"""Tests for the budget auto-requeue cadence (2026-09-28 auto-requeue brief).

Requeues review_runs that ended only because of a budget refusal (empty
Anthropic credit, or the per-author cap) — not a code error — by upserting a
pending_reviews row with trigger_event="manual_requeue" for the poller to pick
up. Uses SQLite in-memory, same pattern as test_poller.py/test_eviction.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import OpsEvent, PendingReview, ReviewRun
from reva.types import JobParams
from scheduler.main import maybe_requeue_budget_failures


def _now() -> datetime:
    return datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _budget_failed_pr(db: Database, *, pr_number: int = 42, ended_ago_hours: int = 3) -> int:
    """Seed an open PR whose latest run failed on an empty Anthropic credit
    balance, ended `ended_ago_hours` ago. Returns the review_run id."""
    repo_id = writers.upsert_repository(
        db, github_repository_id=pr_number, owner="acme", name=f"w{pr_number}",
        default_branch="main", installation_id=99)
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=pr_number, pr_number=pr_number,
        title="Add foo", author_login="alice", base_branch="main", head_branch="f",
        head_sha="deadbeef", state="open", draft=False)
    params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
                       installation_id=99, review_mode="diff", trigger_event="opened")
    run_id = writers.record_review_failed(db, params, "permanent", "Credit balance is too low.")
    with db.session() as s:
        run = s.get(ReviewRun, run_id)
        run.completed_at = _now() - timedelta(hours=ended_ago_hours)
    return run_id


def _ops_events(db: Database) -> list[OpsEvent]:
    with db.session() as s:
        return list(s.query(OpsEvent).filter_by(event="budget_failure_requeued").all())


def _pending_rows(db: Database) -> list[PendingReview]:
    with db.session() as s:
        return list(s.query(PendingReview).all())


def _requeue(db, now, last_run, *, interval_s=900, min_age_s=3600, retry_seconds=3600):
    return maybe_requeue_budget_failures(db, now, last_run, interval_s, min_age_s, retry_seconds)


def test_upserts_one_pending_row_per_listed_run(db):
    run_id = _budget_failed_pr(db, pr_number=42)

    new_last = _requeue(db, _now(), None)

    assert new_last == _now()
    rows = _pending_rows(db)
    assert len(rows) == 1
    assert rows[0].trigger_event == "manual_requeue"
    assert rows[0].pr_number == 42
    assert rows[0].head_sha == "deadbeef"
    assert rows[0].review_mode == "diff"
    assert rows[0].installation_id == 99
    assert rows[0].consumed is False


def test_requeues_multiple_listed_runs_each_with_one_ops_event(db):
    _budget_failed_pr(db, pr_number=42)
    _budget_failed_pr(db, pr_number=43)

    _requeue(db, _now(), None)

    assert len(_pending_rows(db)) == 2
    events = _ops_events(db)
    assert len(events) == 2
    assert {e.detail["pr"] for e in events} == {42, 43}
    assert all(e.component == "scheduler" and e.severity == "info" for e in events)
    assert all(e.detail["status"] == "failed" for e in events)
    assert all("review_run_id" in e.detail and "repo" in e.detail for e in events)


def test_no_op_with_nothing_to_requeue(db):
    new_last = _requeue(db, _now(), None)

    assert new_last == _now()
    assert _pending_rows(db) == []
    assert _ops_events(db) == []


def test_respects_the_interval(db):
    _budget_failed_pr(db, pr_number=42)
    first_now = _now()
    last = _requeue(db, first_now, None)
    assert len(_ops_events(db)) == 1

    # A newer budget-failed PR shows up, but the interval hasn't elapsed yet —
    # the loop must not re-scan (no second ops event for the new PR either).
    _budget_failed_pr(db, pr_number=43)
    second_now = first_now + timedelta(seconds=60)
    last = _requeue(db, second_now, last)

    assert last == first_now  # timer unchanged
    assert len(_ops_events(db)) == 1
    assert len(_pending_rows(db)) == 1


def test_interval_zero_or_negative_disables_the_loop(db):
    _budget_failed_pr(db, pr_number=42)

    last = _requeue(db, _now(), None, interval_s=0)

    assert last is None
    assert _pending_rows(db) == []
    assert _ops_events(db) == []


def test_run_younger_than_min_age_is_not_requeued(db):
    _budget_failed_pr(db, pr_number=42, ended_ago_hours=0)

    _requeue(db, _now(), None)

    assert _pending_rows(db) == []
    assert _ops_events(db) == []


def test_retry_seconds_zero_or_negative_disables_the_loop(db):
    """When budget waiting itself is off (REVA_BUDGET_RETRY_SECONDS <= 0), a
    requeued run would just fail again immediately — the loop must be a
    no-op, not an hourly requeue/decline cycle (Important 4, review
    2026-09-28)."""
    _budget_failed_pr(db, pr_number=42)

    last = _requeue(db, _now(), None, retry_seconds=0)

    assert last is None
    assert _pending_rows(db) == []
    assert _ops_events(db) == []


def test_second_tick_after_requeue_is_a_no_op(db):
    """Critical 1/2 regression: once a run has been requeued, a later tick —
    even past the interval, with the RQ job still unclaimed and the run row
    still failed/declined — must not requeue it a second time. The pending
    row's own scheduled_at is what blocks it (writers.list_budget_ended_reviews)."""
    _budget_failed_pr(db, pr_number=42)
    first_now = _now()
    _requeue(db, first_now, None)
    assert len(_pending_rows(db)) == 1
    assert len(_ops_events(db)) == 1

    second_now = first_now + timedelta(seconds=1000)  # past the interval again
    _requeue(db, second_now, first_now)

    assert len(_pending_rows(db)) == 1  # still just the one requeue
    assert len(_ops_events(db)) == 1
