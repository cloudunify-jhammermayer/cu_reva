"""Tests for the retention purge cadence (F1/SECU-8)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import TicketAnalysis
from reva.types import TicketJobParams
from scheduler.main import maybe_purge_ticket_text


@pytest.fixture()
def db():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _now() -> datetime:
    return datetime(2026, 6, 2, 3, 0, 0, tzinfo=timezone.utc)


def _seed_old_ticket(db) -> int:
    tid = writers.record_ticket_analysis_created(
        db, TicketJobParams(analysis_id=0, ticket_id=1, model_name="helpdesk.ticket",
                            field_name="description", text="raw customer PII")
    )
    with db.session() as s:
        s.get(TicketAnalysis, tid).created_at = datetime.now(timezone.utc) - timedelta(days=40)
    return tid


def test_purge_runs_when_due_and_scrubs(db):
    tid = _seed_old_ticket(db)
    new_last = maybe_purge_ticket_text(db, _now(), None, interval_s=86_400, retention_days=30)
    assert new_last == _now()
    with db.session() as s:
        assert "raw customer PII" not in s.get(TicketAnalysis, tid).input_text


def test_purge_skipped_within_interval(db):
    tid = _seed_old_ticket(db)
    last = _now() - timedelta(seconds=100)  # purged 100s ago, interval is a day
    new_last = maybe_purge_ticket_text(db, _now(), last, interval_s=86_400, retention_days=30)
    assert new_last == last  # not due → unchanged
    with db.session() as s:
        assert s.get(TicketAnalysis, tid).input_text == "raw customer PII"  # untouched
