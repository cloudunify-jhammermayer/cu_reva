"""Tests for the learned-memory distillation cadence (Tier 3 feature B)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scheduler.main import maybe_distill_memories


@dataclass
class FakeQueue:
    enqueued: list[dict] = field(default_factory=list)

    def enqueue(self, func_name: str, *args, **kwargs):
        self.enqueued.append({"func": func_name, "args": args, "kwargs": kwargs})


def _now() -> datetime:
    return datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


def test_distill_enqueued_per_due_repo_when_interval_elapsed():
    q = FakeQueue()
    now = _now()
    last = now - timedelta(seconds=90_000)  # > 1 day
    with patch("scheduler.main.writers.repos_due_for_memory_distill", return_value=[7, 9]):
        new_last = maybe_distill_memories(q, db=None, now=now, last_distill=last,
                                          interval_s=86_400, min_dismissals=3)
    assert [e["func"] for e in q.enqueued] == [
        "worker.memory_distill_runner.run_memory_distill",
        "worker.memory_distill_runner.run_memory_distill",
    ]
    assert [e["args"][0] for e in q.enqueued] == [7, 9]
    assert new_last == now


def test_distill_not_enqueued_within_interval():
    q = FakeQueue()
    now = _now()
    last = now - timedelta(seconds=100)
    with patch("scheduler.main.writers.repos_due_for_memory_distill", return_value=[7]) as due:
        new_last = maybe_distill_memories(q, db=None, now=now, last_distill=last,
                                          interval_s=86_400, min_dismissals=3)
    assert q.enqueued == []
    assert new_last == last
    due.assert_not_called()  # not even queried before the interval elapses


def test_distill_no_due_repos_still_resets_timer():
    q = FakeQueue()
    now = _now()
    with patch("scheduler.main.writers.repos_due_for_memory_distill", return_value=[]):
        new_last = maybe_distill_memories(q, db=None, now=now, last_distill=None,
                                          interval_s=86_400, min_dismissals=3)
    assert q.enqueued == []
    assert new_last == now
