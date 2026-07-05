"""Monthly value-report scheduler cadence."""

from __future__ import annotations

from datetime import datetime, timezone

from scheduler.main import maybe_enqueue_value_report


class FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    def enqueue(self, task: str, params: dict) -> None:
        self.enqueued.append((task, params))


def test_not_due_before_configured_day_and_hour() -> None:
    queue = FakeQueue()
    now = datetime(2026, 7, 1, 6, tzinfo=timezone.utc)

    assert maybe_enqueue_value_report(queue, now, None, day=1, hour=7) is None
    assert queue.enqueued == []


def test_enqueues_previous_month_once_per_month() -> None:
    queue = FakeQueue()
    now = datetime(2026, 7, 1, 7, tzinfo=timezone.utc)

    last = maybe_enqueue_value_report(queue, now, None, day=1, hour=7)
    last_again = maybe_enqueue_value_report(queue, now.replace(day=2), last, day=1, hour=7)

    assert last == now
    assert last_again == last
    assert queue.enqueued == [
        (
            "worker.value_report_tasks.run_value_report",
            {
                "period_start_iso": "2026-06-01T00:00:00+00:00",
                "period_end_iso": "2026-07-01T00:00:00+00:00",
            },
        )
    ]


def test_january_enqueues_december_bounds() -> None:
    queue = FakeQueue()

    maybe_enqueue_value_report(
        queue,
        datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
        None,
        day=1,
        hour=7,
    )

    assert queue.enqueued[0][1] == {
        "period_start_iso": "2025-12-01T00:00:00+00:00",
        "period_end_iso": "2026-01-01T00:00:00+00:00",
    }

