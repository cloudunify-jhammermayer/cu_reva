"""Tests for the repo-cache eviction cadence (INFR-2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from scheduler.main import maybe_enqueue_eviction


@dataclass
class FakeQueue:
    enqueued: list[dict] = field(default_factory=list)

    def enqueue(self, func_name: str, *args, **kwargs):
        self.enqueued.append({"func": func_name, "args": args, "kwargs": kwargs})


def _now() -> datetime:
    return datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


def test_eviction_enqueued_when_interval_elapsed():
    q = FakeQueue()
    now = _now()
    last = now - timedelta(seconds=90_000)  # > 1 day
    new_last = maybe_enqueue_eviction(q, now, last, interval_s=86_400)
    assert len(q.enqueued) == 1
    assert q.enqueued[0]["func"] == "worker.runner.run_repo_cache_eviction"
    assert new_last == now  # timer reset


def test_eviction_not_enqueued_within_interval():
    q = FakeQueue()
    now = _now()
    last = now - timedelta(seconds=100)  # well under a day
    new_last = maybe_enqueue_eviction(q, now, last, interval_s=86_400)
    assert q.enqueued == []
    assert new_last == last  # timer unchanged
