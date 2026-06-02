"""Tests for WeeklyReporter.check_and_send (CONC-2 dedup + TEST-7 coverage)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url
from scheduler.reporter import WeeklyReporter


@dataclass
class FakeQueue:
    enqueued: list = field(default_factory=list)

    def enqueue(self, func_name, *args, **kwargs):
        self.enqueued.append(func_name)


@pytest.fixture()
def db():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _reporter(db, queue, weekday=0, hour=8):
    return WeeklyReporter(db=db, queue=queue, report_weekday=weekday, report_hour_utc=hour)


# A Monday 08:xx UTC (weekday()==0).
_MONDAY_8 = datetime(2026, 6, 1, 8, 30, tzinfo=timezone.utc)


def test_enqueues_on_configured_weekday_and_hour(db):
    q = FakeQueue()
    assert _reporter(db, q).check_and_send(_MONDAY_8) is True
    assert q.enqueued == ["worker.runner.run_weekly_report"]


def test_skips_on_wrong_weekday_when_not_overdue(db):
    q = FakeQueue()
    r = _reporter(db, q)
    r.check_and_send(_MONDAY_8)               # send this week's report
    tuesday = _MONDAY_8 + timedelta(days=1)   # wrong weekday, recent report
    assert r.check_and_send(tuesday) is False  # not overdue → skip
    assert q.enqueued == ["worker.runner.run_weekly_report"]


def test_skips_before_configured_hour(db):
    q = FakeQueue()
    # On the right weekday but before the hour → not due.
    assert _reporter(db, q).check_and_send(_MONDAY_8.replace(hour=7)) is False
    assert q.enqueued == []


def test_fires_later_same_day_if_window_missed(db):
    """CORR-10/INFR-18: a tick after the configured hour on the right weekday
    still fires (no exact-hour requirement)."""
    q = FakeQueue()
    assert _reporter(db, q).check_and_send(_MONDAY_8.replace(hour=20)) is True
    assert q.enqueued == ["worker.runner.run_weekly_report"]


def test_catches_up_when_overdue_on_wrong_weekday(db):
    """CORR-10/INFR-18: if the whole configured weekday was missed, a later day
    still catches up once the report is overdue (≥7 days)."""
    q = FakeQueue()
    r = _reporter(db, q)
    r.check_and_send(_MONDAY_8)  # baseline send
    # 8 days later, a Tuesday, past the hour → overdue → catch up.
    later = _MONDAY_8 + timedelta(days=8)
    assert later.weekday() != 0
    assert r.check_and_send(later) is True
    assert q.enqueued == ["worker.runner.run_weekly_report"] * 2


def test_skips_within_min_interval(db):
    q = FakeQueue()
    r = _reporter(db, q)
    assert r.check_and_send(_MONDAY_8) is True
    # same window, a few seconds later (e.g. another tick) → no second send
    assert r.check_and_send(_MONDAY_8.replace(minute=31)) is False
    assert q.enqueued == ["worker.runner.run_weekly_report"]


def test_enqueues_again_after_interval(db):
    q = FakeQueue()
    r = _reporter(db, q)
    assert r.check_and_send(_MONDAY_8) is True
    next_week = _MONDAY_8 + timedelta(days=7)
    assert r.check_and_send(next_week) is True
    assert q.enqueued == ["worker.runner.run_weekly_report"] * 2
