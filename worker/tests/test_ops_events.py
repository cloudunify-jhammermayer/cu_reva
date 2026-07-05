"""ops_events: safe-to-fail writer + retention purge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import OpsEvent


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def test_record_and_read(db):
    writers.record_ops_event(
        db, "codegraph", "warning", "index_failed",
        {"repo": "acme/widgets", "error": "exit 1"},
    )
    with db.session() as s:
        row = s.query(OpsEvent).one()
    assert row.component == "codegraph"
    assert row.severity == "warning"
    assert row.event == "index_failed"
    assert row.detail["repo"] == "acme/widgets"
    assert row.created_at is not None


def test_detail_optional(db):
    writers.record_ops_event(db, "git", "warning", "fetch_timeout")
    with db.session() as s:
        assert s.query(OpsEvent).one().detail is None


def test_writer_swallows_db_failure(db, monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "session", boom)
    writers.record_ops_event(db, "codegraph", "error", "index_failed", {})


def test_purge_old_events(db):
    writers.record_ops_event(db, "git", "warning", "old")
    with db.session() as s:
        s.query(OpsEvent).one().created_at = (
            datetime.now(timezone.utc) - timedelta(days=40)
        )
    writers.record_ops_event(db, "git", "warning", "fresh")

    assert writers.purge_old_ops_events(db, older_than_days=30) == 1
    with db.session() as s:
        assert s.query(OpsEvent).one().event == "fresh"
    assert writers.purge_old_ops_events(db, older_than_days=30) == 0
