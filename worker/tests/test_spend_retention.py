"""claude_spend rows are deleted past the retention window."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import ClaudeSpend


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _spend_row(db: Database, days_old: int) -> None:
    with db.session() as s:
        s.add(ClaudeSpend(
            kind="review", cost_usd=1.0,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_old),
        ))


def test_purges_only_rows_past_window(db):
    _spend_row(db, days_old=500)
    _spend_row(db, days_old=10)
    deleted = writers.purge_old_claude_spend(db, older_than_days=400)
    assert deleted == 1
    with db.session() as s:
        assert s.query(ClaudeSpend).count() == 1


def test_idempotent(db):
    _spend_row(db, days_old=500)
    assert writers.purge_old_claude_spend(db, 400) == 1
    assert writers.purge_old_claude_spend(db, 400) == 0
