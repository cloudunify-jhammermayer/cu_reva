"""Process-wide configuration for the scheduler container."""

from __future__ import annotations

import os
from dataclasses import dataclass

from reva.claude_code_runner import REVIEW_JOB_TIMEOUT
from reva.config import required_env_or_file

# Reap runs stuck in 'running' past 2× the job timeout — far beyond any legit
# (possibly retried) review, so only crashed/SIGKILLed workers get swept.
_DEFAULT_STALE_RUNNING_SECONDS = 2 * REVIEW_JOB_TIMEOUT


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    migrations_dir: str = "/app/db/migrations"
    queue_name: str = "reviews"
    poll_interval_seconds: int = 30
    stale_running_seconds: int = _DEFAULT_STALE_RUNNING_SECONDS
    # Touched each loop iteration; the container healthcheck checks its freshness
    # to detect a hung scheduler.
    heartbeat_path: str = "/tmp/reva-scheduler-heartbeat"
    report_weekday: int = 0      # 0=Monday
    report_hour_utc: int = 8     # hour to fire the weekly report (UTC)
    # Operational alerting (Google Chat). Empty webhook = alerts disabled.
    google_chat_webhook_url: str = ""
    queue_depth_alert: int = 50          # alert when the queue backs up past this
    failed_jobs_alert: int = 10          # alert when RQ's failed registry exceeds this
    repo_cache_disk_pct_alert: int = 90  # alert when the repo-cache filesystem is this % full
    repo_cache_dir: str = "/repos"
    # How often to enqueue a repo-cache eviction job (INFR-2). Daily by default.
    eviction_interval_seconds: int = 86_400
    # Data retention (F1/SECU-8): scrub raw customer ticket text older than this,
    # checked on the same daily cadence.
    ticket_text_retention_days: int = 30
    retention_purge_interval_seconds: int = 86_400
    # Per-repo learned-memory distillation (Tier 3 feature B). Daily by default;
    # a repo is due when it has >= this many dismissals with newer signal than
    # its active memory version.
    memory_distill_interval_seconds: int = 86_400
    memory_distill_min_dismissals: int = 3

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=required_env_or_file("DATABASE_URL"),
            redis_url=required_env_or_file("REDIS_URL"),
            migrations_dir=os.environ.get("REVA_MIGRATIONS_DIR", "/app/db/migrations"),
            queue_name=os.environ.get("REVA_QUEUE_NAME", "reviews"),
            poll_interval_seconds=int(os.environ.get("REVA_POLL_INTERVAL_SECONDS", "30")),
            stale_running_seconds=int(
                os.environ.get("REVA_STALE_RUNNING_SECONDS", str(_DEFAULT_STALE_RUNNING_SECONDS))
            ),
            heartbeat_path=os.environ.get(
                "REVA_SCHEDULER_HEARTBEAT_PATH", "/tmp/reva-scheduler-heartbeat"
            ),
            report_weekday=int(os.environ.get("REVA_REPORT_WEEKDAY", "0")),
            report_hour_utc=int(os.environ.get("REVA_REPORT_HOUR_UTC", "8")),
            google_chat_webhook_url=os.environ.get("GOOGLE_CHAT_WEBHOOK_URL", ""),
            queue_depth_alert=int(os.environ.get("REVA_QUEUE_DEPTH_ALERT", "50")),
            failed_jobs_alert=int(os.environ.get("REVA_FAILED_JOBS_ALERT", "10")),
            repo_cache_disk_pct_alert=int(os.environ.get("REVA_REPO_CACHE_DISK_PCT_ALERT", "90")),
            repo_cache_dir=os.environ.get("REVA_REPO_CACHE_DIR", "/repos"),
            eviction_interval_seconds=int(
                os.environ.get("REVA_EVICTION_INTERVAL_SECONDS", "86400")
            ),
            ticket_text_retention_days=int(
                os.environ.get("REVA_TICKET_TEXT_RETENTION_DAYS", "30")
            ),
            retention_purge_interval_seconds=int(
                os.environ.get("REVA_RETENTION_PURGE_INTERVAL_SECONDS", "86400")
            ),
            memory_distill_interval_seconds=int(
                os.environ.get("REVA_MEMORY_DISTILL_INTERVAL_SECONDS", "86400")
            ),
            memory_distill_min_dismissals=int(
                os.environ.get("REVA_MEMORY_DISTILL_MIN_DISMISSALS", "3")
            ),
        )
