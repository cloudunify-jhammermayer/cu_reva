"""Real-Postgres integration tests for scheduler concurrency guards (D1/TEST-1).

Skipped unless REVA_TEST_POSTGRES_URL is set (see worker/tests/test_pg_integration.py).
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from reva.db.engine import Database, create_engine_from_url
from scheduler.reporter import WeeklyReporter

PG_URL = os.environ.get("REVA_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="REVA_TEST_POSTGRES_URL not set (real-Postgres integration tier)"
)

_MIGRATIONS_DIR = str(Path(__file__).resolve().parents[2] / "db" / "migrations")


@dataclass
class FakeQueue:
    enqueued: list = field(default_factory=list)

    def enqueue(self, func_name, *args, **kwargs):
        self.enqueued.append(func_name)


@pytest.fixture()
def pg_db():
    db = Database(create_engine_from_url(PG_URL))
    db.migrate(_MIGRATIONS_DIR)
    with db.engine.begin() as conn:
        conn.execute(text("TRUNCATE weekly_reports RESTART IDENTITY"))
    yield db
    db.engine.dispose()


def test_weekly_report_not_double_sent_by_concurrent_replicas(pg_db):
    """CONC-2: two scheduler replicas hitting check_and_send in the same window
    must not both send. The advisory lock + committed dedup row guarantee exactly
    one enqueue (on SQLite the lock no-ops, so this is the real-PG proof)."""
    now = datetime(2026, 6, 1, 8, 30, tzinfo=timezone.utc)  # Monday 08:30 UTC
    barrier = threading.Barrier(2)
    sent: list[bool] = []
    lock = threading.Lock()

    def replica():
        # Each replica is its own reporter + queue, sharing the same DB.
        reporter = WeeklyReporter(db=pg_db, queue=FakeQueue(), report_weekday=0, report_hour_utc=8)
        barrier.wait()
        result = reporter.check_and_send(now)
        with lock:
            sent.append(result)

    t1 = threading.Thread(target=replica)
    t2 = threading.Thread(target=replica)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sum(sent) == 1, f"exactly one replica must send, got {sent}"
    with pg_db.session() as s:
        count = s.execute(text("SELECT count(*) FROM weekly_reports")).scalar_one()
    assert count == 1, "exactly one weekly_reports dedup row must exist"
