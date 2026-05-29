"""Tests for the operational Monitor — threshold alerts with transition dedup."""

from __future__ import annotations

from unittest.mock import patch

from scheduler.monitor import Monitor


class FakeQueue:
    def __init__(self, depth=0):
        self.depth = depth

    def __len__(self):
        return self.depth


def _monitor(queue, **over):
    kw = dict(
        queue_depth_alert=50,
        failed_jobs_alert=10,
        repo_cache_disk_pct_alert=90,
        repo_cache_dir="/nonexistent-so-disk-check-skips",
    )
    kw.update(over)
    return Monitor(queue, "https://chat.example/webhook", **kw)


def test_alerts_once_on_breach_not_every_tick():
    m = _monitor(FakeQueue(depth=100))
    with patch("scheduler.monitor.notify_operational_alert") as notify, \
         patch.object(Monitor, "_failed_count", return_value=0):
        m.check()
        m.check()  # still breached — must NOT re-alert
    assert notify.call_count == 1
    assert "queue" in notify.call_args[0][1].lower()


def test_recovery_alert_when_clearing():
    q = FakeQueue(depth=100)
    m = _monitor(q)
    with patch("scheduler.monitor.notify_operational_alert") as notify, \
         patch.object(Monitor, "_failed_count", return_value=0):
        m.check()           # breach -> alert
        q.depth = 0
        m.check()           # cleared -> recovery alert
    assert notify.call_count == 2
    assert "recovered" in notify.call_args[0][1].lower()


def test_no_alert_below_threshold():
    m = _monitor(FakeQueue(depth=5))
    with patch("scheduler.monitor.notify_operational_alert") as notify, \
         patch.object(Monitor, "_failed_count", return_value=0):
        m.check()
    notify.assert_not_called()


def test_disabled_webhook_is_safe():
    m = Monitor(FakeQueue(depth=100), "", queue_depth_alert=50, failed_jobs_alert=10,
                repo_cache_disk_pct_alert=90, repo_cache_dir="/nope")
    with patch.object(Monitor, "_failed_count", return_value=0):
        m.check()  # notify_operational_alert no-ops on empty webhook; must not raise
