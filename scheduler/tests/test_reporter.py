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


def test_skips_on_wrong_weekday(db):
    q = FakeQueue()
    tuesday = _MONDAY_8 + timedelta(days=1)
    assert _reporter(db, q).check_and_send(tuesday) is False
    assert q.enqueued == []


def test_skips_on_wrong_hour(db):
    q = FakeQueue()
    assert _reporter(db, q).check_and_send(_MONDAY_8.replace(hour=9)) is False
    assert q.enqueued == []


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
