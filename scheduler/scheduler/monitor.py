"""Operational monitoring — posts Google Chat alerts when thresholds are crossed.

Runs each scheduler tick. Alerts fire on *transition* into a breach (and a
recovery note on clearing), tracked in memory, so a sustained problem doesn't
spam the channel every cycle.

ASSUMES A SINGLE SCHEDULER REPLICA (CONC-10). The breach state (`self._firing`)
is per-process and in-memory, so running N schedulers would fire/recover each
alert N times (edge-triggering is local, not global). REVA scales workers, not
schedulers; if you ever run >1 scheduler, move `_firing` to shared storage
(Redis/DB keyed by metric) or elect a single monitoring leader first.
"""

from __future__ import annotations

import os
import shutil

import structlog

from reva.notifications import notify_operational_alert

logger = structlog.get_logger()


class Monitor:
    def __init__(
        self,
        queue,
        webhook_url: str,
        *,
        queue_depth_alert: int,
        failed_jobs_alert: int,
        repo_cache_disk_pct_alert: int,
        repo_cache_dir: str,
    ) -> None:
        self.queue = queue
        self.webhook_url = webhook_url
        self.queue_depth_alert = queue_depth_alert
        self.failed_jobs_alert = failed_jobs_alert
        self.repo_cache_disk_pct_alert = repo_cache_disk_pct_alert
        self.repo_cache_dir = repo_cache_dir
        self._firing: set[str] = set()

    def check(self) -> None:
        self._evaluate(
            "queue_depth", self._queue_depth(), self.queue_depth_alert,
            "Review queue backed up", "jobs queued",
        )
        self._evaluate(
            "failed_jobs", self._failed_count(), self.failed_jobs_alert,
            "Failed review jobs piling up", "jobs in the failed registry",
        )
        self._evaluate(
            "disk", self._disk_pct(), self.repo_cache_disk_pct_alert,
            "Repo-cache disk almost full", "% used on the repo-cache volume",
        )

    # ---------------------------------------------------------------- internals

    def _evaluate(self, key: str, value: int | None, threshold: int, title: str, unit: str) -> None:
        if value is None:
            return
        if value >= threshold and key not in self._firing:
            # Mark firing only if the alert was actually delivered — otherwise a
            # Chat outage at the transition swallows it forever (the next message
            # would be a spurious "Recovered"). A failed send leaves key unset so
            # the next tick re-attempts while the breach persists.
            if notify_operational_alert(
                self.webhook_url, title, f"{value} {unit} (threshold {threshold})."
            ):
                self._firing.add(key)
            logger.warning("operational_alert", metric=key, value=value, threshold=threshold)
        elif value < threshold and key in self._firing:
            self._firing.discard(key)
            notify_operational_alert(
                self.webhook_url, f"Recovered: {title}", f"Back to {value} {unit}."
            )

    def _queue_depth(self) -> int | None:
        try:
            return len(self.queue)
        except Exception:
            return None

    def _failed_count(self) -> int | None:
        try:
            from rq.registry import FailedJobRegistry
            return FailedJobRegistry(queue=self.queue).count
        except Exception:
            return None

    def _disk_pct(self) -> int | None:
        try:
            if not os.path.isdir(self.repo_cache_dir):
                return None
            usage = shutil.disk_usage(self.repo_cache_dir)
            return int(usage.used / usage.total * 100)
        except Exception:
            return None
