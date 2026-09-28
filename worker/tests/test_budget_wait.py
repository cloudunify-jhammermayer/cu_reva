"""defer_for_budget: the shared wait-and-resume gate (spec 2026-09-27)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from reva.db import Base, Database, create_engine_from_url
from reva.db.models import OpsEvent
from worker.runner import WorkerContext, defer_for_budget


def _db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _ctx(queue=None, retry=900, max_wait=86400) -> WorkerContext:
    return WorkerContext(
        db=_db(), claude=None, runner=None, github=None,  # type: ignore[arg-type]
        reviewer=None, auditor=None, ticket_analyzer=None, verifier=None,  # type: ignore[arg-type]
        rq_queue=queue, budget_retry_seconds=retry, budget_wait_max_seconds=max_wait,
    )


def _events(ctx) -> list[tuple[str, str, str]]:
    with ctx.db.session() as s:
        return [(e.component, e.severity, e.event) for e in s.query(OpsEvent).all()]


def _queue():
    q = MagicMock()
    q.enqueue_in.return_value = MagicMock(id="rq:job:deferred-1")
    return q


def test_first_wait_stamps_since_and_enqueues_with_retry():
    q = _queue()
    ctx = _ctx(q)
    log = MagicMock()

    out = defer_for_budget(ctx, "worker.audit_tasks.run_audit", {"repository_id": 1},
                           kind="audit", spent=12.5, log=log, job_timeout=1234)

    assert out["status"] == "waiting_budget"
    assert out["retry_job_id"] == "rq:job:deferred-1"
    assert out["retry_in_seconds"] == 900
    delay, task, params = q.enqueue_in.call_args.args
    assert delay == timedelta(seconds=900)
    assert task == "worker.audit_tasks.run_audit"
    assert params["repository_id"] == 1
    since = datetime.fromisoformat(params["budget_wait_since"])
    assert since.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - since).total_seconds()) < 5
    kwargs = q.enqueue_in.call_args.kwargs
    assert kwargs["job_timeout"] == 1234
    assert kwargs["retry"].max == 3
    assert _events(ctx) == [("audit", "warning", "budget_wait_started")]


def test_reason_defaults_to_cap_in_dict_params_and_event_detail():
    q = _queue()
    ctx = _ctx(q)

    out = defer_for_budget(ctx, "worker.audit_tasks.run_audit", {}, kind="audit",
                           spent=1.0, log=MagicMock())

    assert out["reason"] == "cap"
    params = q.enqueue_in.call_args.args[2]
    assert params["budget_wait_reason"] == "cap"
    with ctx.db.session() as s:
        event = s.query(OpsEvent).one()
        assert event.severity == "warning"
        assert event.detail["reason"] == "cap"


def test_provider_credit_reason_uses_error_severity_and_is_threaded_through():
    q = _queue()
    ctx = _ctx(q)
    log = MagicMock()

    out = defer_for_budget(ctx, "worker.tasks.run_review", {}, kind="review",
                           spent=0.0, log=log, reason="provider_credit")

    assert out["status"] == "waiting_budget"
    assert out["reason"] == "provider_credit"
    params = q.enqueue_in.call_args.args[2]
    assert params["budget_wait_reason"] == "provider_credit"
    with ctx.db.session() as s:
        event = s.query(OpsEvent).one()
        assert event.event == "budget_wait_started"
        assert event.severity == "error"
        assert event.detail["reason"] == "provider_credit"


def test_provider_credit_expiry_records_reason_in_event_detail():
    q = _queue()
    ctx = _ctx(q, max_wait=3600)
    since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    out = defer_for_budget(ctx, "t", {"budget_wait_since": since}, kind="review",
                           spent=1.0, log=MagicMock(), reason="provider_credit")

    assert out is None
    with ctx.db.session() as s:
        event = s.query(OpsEvent).one()
        assert event.event == "budget_wait_expired"
        assert event.detail["reason"] == "provider_credit"


def test_repeat_wait_keeps_original_since_and_records_no_event():
    q = _queue()
    ctx = _ctx(q)
    since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    out = defer_for_budget(ctx, "t", {"budget_wait_since": since}, kind="audit",
                           spent=1.0, log=MagicMock())

    assert out["budget_wait_since"] == since
    assert q.enqueue_in.call_args.args[2]["budget_wait_since"] == since
    assert _events(ctx) == []


def test_defer_parses_string_and_naive_since():
    """RQ hands the dict back as pickled; a naive datetime or a string must both
    be treated as UTC rather than crash the gate (Review Focus 1)."""
    q = _queue()
    ctx = _ctx(q)
    naive = datetime.now() - timedelta(minutes=5)

    out = defer_for_budget(ctx, "t", {"budget_wait_since": naive}, kind="audit",
                           spent=1.0, log=MagicMock())

    assert out is not None
    assert datetime.fromisoformat(out["budget_wait_since"]).tzinfo is not None


def test_expired_wait_returns_none_with_error_event():
    q = _queue()
    ctx = _ctx(q, max_wait=3600)
    since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    out = defer_for_budget(ctx, "t", {"budget_wait_since": since}, kind="review",
                           spent=1.0, log=MagicMock())

    assert out is None
    q.enqueue_in.assert_not_called()
    assert _events(ctx) == [("review", "error", "budget_wait_expired")]


def test_disabled_or_no_queue_returns_none_silently():
    assert defer_for_budget(_ctx(_queue(), retry=0), "t", {}, kind="audit",
                            spent=1.0, log=MagicMock()) is None
    ctx = _ctx(None)
    assert defer_for_budget(ctx, "t", {}, kind="audit", spent=1.0, log=MagicMock()) is None
    assert _events(ctx) == []


def test_enqueue_failure_returns_none_with_error_event():
    q = _queue()
    q.enqueue_in.side_effect = ConnectionError("redis down")
    ctx = _ctx(q)

    out = defer_for_budget(ctx, "t", {}, kind="comment_reply", spent=1.0, log=MagicMock())

    assert out is None
    assert _events(ctx) == [("comment_reply", "error", "budget_wait_enqueue_failed")]


def test_job_timeout_taken_from_current_rq_job(monkeypatch):
    q = _queue()
    ctx = _ctx(q)
    job = MagicMock(timeout=2100, failure_ttl=86400)
    monkeypatch.setattr("worker.runner.get_current_job", lambda: job)

    defer_for_budget(ctx, "t", {}, kind="audit", spent=1.0, log=MagicMock(), job_timeout=5)

    kwargs = q.enqueue_in.call_args.kwargs
    assert kwargs["job_timeout"] == 2100
    assert kwargs["failure_ttl"] == 86400


def test_settings_defaults_and_env(monkeypatch, tmp_path):
    from worker.settings import Settings
    key = tmp_path / "key.pem"
    key.write_text("pem")
    for var in ("REDIS_URL", "DATABASE_URL", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(var, "x")
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_PATH", str(key))
    monkeypatch.delenv("REVA_BUDGET_RETRY_SECONDS", raising=False)
    monkeypatch.delenv("REVA_BUDGET_WAIT_MAX_SECONDS", raising=False)
    s = Settings.from_env()
    assert (s.budget_retry_seconds, s.budget_wait_max_seconds) == (3600, 172800)

    monkeypatch.setenv("REVA_BUDGET_RETRY_SECONDS", "0")
    monkeypatch.setenv("REVA_BUDGET_WAIT_MAX_SECONDS", "3600")
    s = Settings.from_env()
    assert (s.budget_retry_seconds, s.budget_wait_max_seconds) == (0, 3600)


def test_params_models_accept_budget_wait_since():
    from reva.types import (AuditJobParams, JobParams, SupportJobParams,
                            TicketIssueJobParams, TicketJobParams, TimesheetJobParams)
    iso = "2026-09-27T10:00:00+00:00"
    assert JobParams(repository_id=1, pull_request_id=1, head_sha="a", installation_id=1,
                     trigger_event="opened", budget_wait_since=iso).budget_wait_since.tzinfo
    assert AuditJobParams(repository_id=1, installation_id=1).budget_wait_since is None
    for model in (TicketJobParams, SupportJobParams, TicketIssueJobParams, TimesheetJobParams):
        assert "budget_wait_since" in model.model_fields
