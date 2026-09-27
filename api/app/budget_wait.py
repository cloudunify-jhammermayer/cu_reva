"""Requeue safety for rows that are waiting for budget (spec 2026-09-27).

A waiting row has a live scheduled job in Redis that will re-run it; letting
an ops/Odoo requeue start a second job would double-pay. The exemption ends
once the worker's maximum wait plus the route's own stale window has passed,
so a wait lost to a Redis flush still becomes requeueable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

WAITING_DETAIL = "Waiting for budget; retries automatically"


def is_waiting_for_budget(row: dict, stale_window: timedelta, max_wait_seconds: int) -> bool:
    since = row.get("budget_wait_since")
    if since is None:
        return False
    if row.get("status") not in (None, "pending"):
        # A given-up (failed) or completed row is never "waiting" — its marker
        # is stale and must not block a requeue or the create-dedup check.
        return False
    if since.tzinfo is None:  # SQLite returns naive datetimes
        since = since.replace(tzinfo=timezone.utc)
    return since > datetime.now(timezone.utc) - timedelta(seconds=max_wait_seconds) - stale_window
