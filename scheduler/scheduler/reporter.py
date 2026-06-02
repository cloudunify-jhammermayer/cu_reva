"""Weekly report scheduler.

Fires once per week on the configured weekday + hour (UTC). Uses the
`weekly_reports` DB table to avoid double-sending across restarts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from rq import Queue
from sqlalchemy import select, text

from reva.db.engine import Database
from reva.db.models import WeeklyReport

logger = structlog.get_logger()

_MIN_INTERVAL = timedelta(days=6)
# Transaction-level advisory lock so concurrent scheduler replicas evaluate the
# "send the weekly report?" decision one at a time (CONC-2). Distinct from the
# budget cap's key. No-op on SQLite.
_REPORT_ADVISORY_LOCK_KEY = 0x52455750  # "REWP"


class WeeklyReporter:
    def __init__(
        self,
        db: Database,
        queue: Queue,
        report_weekday: int = 0,   # 0=Monday
        report_hour_utc: int = 8,
    ) -> None:
        self._db = db
        self._queue = queue
        self._report_weekday = report_weekday
        self._report_hour_utc = report_hour_utc

    def check_and_send(self, now: datetime) -> bool:
        """Enqueue a weekly report if it's time. Returns True if enqueued.

        CONC-2: the read-check-record step runs under a transaction-level advisory
        lock and commits the `weekly_reports` row BEFORE enqueuing, so concurrent
        scheduler replicas in the same hour can't both send. The committed row is
        the dedup token; the second replica re-reads it under the lock and skips.
        """
        if now.weekday() != self._report_weekday:
            return False
        if now.hour != self._report_hour_utc:
            return False

        if not self._claim_period(now):
            return False

        # Enqueue only after the dedup row is committed: a crash here means a
        # missed report (recoverable next tick), never a duplicate.
        self._queue.enqueue("worker.runner.run_weekly_report", {})
        logger.info("weekly_report_enqueued", weekday=now.weekday(), hour=now.hour)
        return True

    def _claim_period(self, now: datetime) -> bool:
        """Record this report period if no recent one exists; True if claimed.

        Atomic across replicas: the advisory lock serializes the read+insert, so
        only the first replica in the window inserts and returns True.
        """
        with self._db.session() as s:
            if s.get_bind().dialect.name == "postgresql":
                s.execute(
                    text("SELECT pg_advisory_xact_lock(:k)"),
                    {"k": _REPORT_ADVISORY_LOCK_KEY},
                )
            # ORM read/insert so SQLAlchemy coerces enqueued_at to a datetime and
            # applies the period_days default on both dialects (raw SQL did not).
            last = s.execute(
                select(WeeklyReport.enqueued_at)
                .order_by(WeeklyReport.enqueued_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if last is not None:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (now - last) < _MIN_INTERVAL:
                    return False
            s.add(WeeklyReport(enqueued_at=now))
        return True
