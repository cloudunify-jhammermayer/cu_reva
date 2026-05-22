"""Weekly report scheduler.

Fires once per week on the configured weekday + hour (UTC). Uses the
`weekly_reports` DB table to avoid double-sending across restarts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from rq import Queue
from sqlalchemy import text

from reva.db.engine import Database

logger = structlog.get_logger()

_MIN_INTERVAL = timedelta(days=6)


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
        """Enqueue a weekly report if it's time. Returns True if enqueued."""
        if now.weekday() != self._report_weekday:
            return False
        if now.hour != self._report_hour_utc:
            return False

        last = self._last_enqueued_at()
        if last is not None and (now - last) < _MIN_INTERVAL:
            return False

        self._queue.enqueue("worker.runner.run_weekly_report", {})
        self._record_enqueued(now)
        logger.info("weekly_report_enqueued", weekday=now.weekday(), hour=now.hour)
        return True

    def _last_enqueued_at(self) -> datetime | None:
        with self._db.session() as s:
            row = s.execute(
                text("SELECT enqueued_at FROM weekly_reports ORDER BY enqueued_at DESC LIMIT 1")
            ).first()
        if row is None:
            return None
        t = row[0]
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t

    def _record_enqueued(self, now: datetime) -> None:
        with self._db.session() as s:
            s.execute(
                text("INSERT INTO weekly_reports (enqueued_at) VALUES (:t)"),
                {"t": now},
            )
