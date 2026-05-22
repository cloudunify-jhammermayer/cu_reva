"""Process-wide configuration for the scheduler container."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    migrations_dir: str = "/app/db/migrations"
    queue_name: str = "reviews"
    poll_interval_seconds: int = 30
    report_weekday: int = 0      # 0=Monday
    report_hour_utc: int = 8     # hour to fire the weekly report (UTC)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ["DATABASE_URL"],
            redis_url=os.environ["REDIS_URL"],
            migrations_dir=os.environ.get("REVA_MIGRATIONS_DIR", "/app/db/migrations"),
            queue_name=os.environ.get("REVA_QUEUE_NAME", "reviews"),
            poll_interval_seconds=int(os.environ.get("REVA_POLL_INTERVAL_SECONDS", "30")),
            report_weekday=int(os.environ.get("REVA_REPORT_WEEKDAY", "0")),
            report_hour_utc=int(os.environ.get("REVA_REPORT_HOUR_UTC", "8")),
        )
