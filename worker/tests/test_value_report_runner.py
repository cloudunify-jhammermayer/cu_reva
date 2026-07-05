"""Monthly value-report worker job."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import OpsEvent
from worker.runner import WorkerContext, set_context
from worker.value_report_runner import run_value_report

_PARAMS = {
    "period_start_iso": datetime(2026, 6, 1, tzinfo=timezone.utc).isoformat(),
    "period_end_iso": datetime(2026, 7, 1, tzinfo=timezone.utc).isoformat(),
}


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _set_worker_context(db: Database, *, chat_enabled: bool) -> None:
    set_context(WorkerContext(
        db=db,
        claude=None,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        github=None,  # type: ignore[arg-type]
        reviewer=None,  # type: ignore[arg-type]
        auditor=None,  # type: ignore[arg-type]
        ticket_analyzer=None,  # type: ignore[arg-type]
        verifier=None,  # type: ignore[arg-type]
        google_chat_webhook_url="https://chat.example/webhook",
        value_report_chat_enabled=chat_enabled,
    ))


def test_default_chat_off_persists_only(db: Database, monkeypatch) -> None:
    _set_worker_context(db, chat_enabled=False)
    calls = []

    def fake_notify(webhook_url: str, summary: str) -> None:
        calls.append((webhook_url, summary))

    monkeypatch.setattr("worker.value_report_runner.notify_value_report", fake_notify)

    out = run_value_report(_PARAMS)

    assert out["status"] == "persisted"
    assert calls == []
    row = writers.get_value_reports(db, limit=1)[0]
    assert row["chat_sent"] is False


def test_chat_enabled_sends_and_marks_sent(db: Database, monkeypatch) -> None:
    _set_worker_context(db, chat_enabled=True)
    calls = []

    def fake_notify(webhook_url: str, summary: str) -> None:
        calls.append((webhook_url, summary))

    monkeypatch.setattr("worker.value_report_runner.notify_value_report", fake_notify)

    out = run_value_report(_PARAMS)

    assert out["status"] == "sent"
    assert calls and calls[0][0] == "https://chat.example/webhook"
    row = writers.get_value_reports(db, limit=1)[0]
    assert row["chat_sent"] is True


def test_chat_failure_keeps_report_and_records_ops_event(db: Database, monkeypatch) -> None:
    _set_worker_context(db, chat_enabled=True)

    def fail_notify(_webhook_url: str, _summary: str) -> None:
        raise RuntimeError("chat down")

    monkeypatch.setattr("worker.value_report_runner.notify_value_report", fail_notify)

    out = run_value_report(_PARAMS)

    assert out["status"] == "persisted_chat_failed"
    assert writers.get_value_reports(db, limit=1)[0]["chat_sent"] is False
    with db.session() as s:
        event = s.query(OpsEvent).one()
    assert event.component == "value_report"
    assert event.event == "chat_failed"
