"""Tests for timesheet_runner.run_timesheet_review."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.errors import PermanentError, TransientError
from reva.types import ClaudeResponse, TimesheetJobParams, TimesheetLine, TimesheetLineResult
from worker.runner import WorkerContext, set_context
from worker.timesheet_runner import run_timesheet_review


@dataclass
class FakeTimesheetAnalyzer:
    raise_exc: Exception | None = None
    result_batches: list[list[TimesheetLineResult]] | None = None
    calls: list[list[int]] = field(default_factory=list)

    def analyze_chunk(self, lines, flagged_words):
        self.calls.append([line.line_id for line in lines])
        if self.raise_exc:
            raise self.raise_exc
        if self.result_batches is not None:
            results = self.result_batches.pop(0)
            return _response(), results
        results = [
            TimesheetLineResult(line_id=lines[0].line_id, status="ok"),
            TimesheetLineResult(
                line_id=lines[1].line_id,
                status="rewritten",
                updated_desc="Implemented report fixes",
            ),
        ][: len(lines)]
        return _response(), results


@dataclass
class FakeOdoo:
    raise_exc: Exception | None = None
    calls: list[dict] = field(default_factory=list)

    def timesheet_results(self, request_id, results, stats):
        self.calls.append({"request_id": request_id, "results": results, "stats": stats})
        if self.raise_exc:
            raise self.raise_exc


def _response() -> ClaudeResponse:
    return ClaudeResponse(
        model="claude-sonnet-4-6",
        stop_reason="tool_use",
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=0,
        cache_creation_tokens=0,
    )


def _line(line_id: int) -> TimesheetLine:
    return TimesheetLine(
        line_id=line_id,
        project_name="ACME",
        task_name="Reports",
        user_name="Jo",
        user_role="developer",
        description="fixed stupid bug",
    )


def _params(db: Database) -> dict:
    stub = TimesheetJobParams(
        run_id=0,
        odoo_instance_id=1,
        request_id="req-1",
        flagged_words=["stupid"],
        lines=[_line(1), _line(2)],
    )
    run_id = writers.record_timesheet_run_created(db, stub)
    writers.attach_timesheet_job_id(db, run_id, "rq:job:test")
    return TimesheetJobParams(
        run_id=run_id,
        odoo_instance_id=stub.odoo_instance_id,
        request_id=stub.request_id,
        flagged_words=stub.flagged_words,
        lines=stub.lines,
    ).model_dump()


@pytest.fixture()
def ctx_and_fakes(monkeypatch):
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)
    analyzer = FakeTimesheetAnalyzer()
    odoo = FakeOdoo()
    ctx = WorkerContext(
        db=db,
        claude=None,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        github=None,  # type: ignore[arg-type]
        reviewer=None,  # type: ignore[arg-type]
        auditor=None,  # type: ignore[arg-type]
        ticket_analyzer=None,  # type: ignore[arg-type]
        verifier=None,  # type: ignore[arg-type]
        timesheet_analyzer=analyzer,  # type: ignore[arg-type]
    )
    monkeypatch.setattr("worker.timesheet_runner.build_odoo_client", lambda ctx, _id: odoo)
    set_context(ctx)
    return {"ctx": ctx, "db": db, "analyzer": analyzer, "odoo": odoo}


def test_happy_path_persists_and_callbacks(ctx_and_fakes):
    s = ctx_and_fakes
    params = _params(s["db"])

    out = run_timesheet_review(params)

    assert out == {"status": "completed", "run_id": params["run_id"]}
    row = writers.get_timesheet_run(s["db"], params["run_id"])
    assert row["status"] == "completed"
    assert row["callback_payload"] is None
    assert row["callback_sent_at"] is not None
    assert (row["ok_count"], row["rewritten_count"]) == (1, 1)
    assert s["odoo"].calls[0]["results"] == [
        {"line_id": 2, "status": "rewritten", "updated_desc": "Implemented report fixes"}
    ]


def test_retry_after_callback_failure_does_not_reanalyze(ctx_and_fakes):
    s = ctx_and_fakes
    params = _params(s["db"])

    s["odoo"].raise_exc = TransientError("Odoo 503")
    with pytest.raises(TransientError):
        run_timesheet_review(params)
    assert len(s["analyzer"].calls) == 1
    assert writers.get_timesheet_run(s["db"], params["run_id"])["status"] == "completed"

    s["odoo"].raise_exc = None
    run_timesheet_review(params)
    assert len(s["analyzer"].calls) == 1
    assert len(s["odoo"].calls) == 2


def test_permanent_callback_marks_failed_and_keeps_payload(ctx_and_fakes):
    s = ctx_and_fakes
    s["odoo"].raise_exc = PermanentError("Odoo 409")
    params = _params(s["db"])

    with pytest.raises(PermanentError):
        run_timesheet_review(params)

    row = writers.get_timesheet_run(s["db"], params["run_id"])
    assert row["status"] == "failed"
    assert "Odoo 409" in row["error_message"]
    assert row["callback_payload"]["results"] == [
        {"line_id": 2, "status": "rewritten", "updated_desc": "Implemented report fixes"}
    ]


def test_missing_line_gets_one_coverage_retry(ctx_and_fakes):
    s = ctx_and_fakes
    s["analyzer"].result_batches = [
        [TimesheetLineResult(line_id=1, status="ok")],
        [TimesheetLineResult(line_id=2, status="needs_human", reason="no context")],
    ]
    params = _params(s["db"])

    run_timesheet_review(params)

    assert s["analyzer"].calls == [[1, 2], [2]]
    assert s["odoo"].calls[0]["results"] == [
        {"line_id": 2, "status": "needs_human", "reason": "no context"}
    ]


def test_identical_rewrite_downgrades_to_ok(ctx_and_fakes):
    s = ctx_and_fakes
    s["analyzer"].result_batches = [
        [
            TimesheetLineResult(
                line_id=1,
                status="rewritten",
                updated_desc="fixed stupid bug",
            ),
            TimesheetLineResult(line_id=2, status="ok"),
        ]
    ]
    params = _params(s["db"])

    run_timesheet_review(params)

    assert s["odoo"].calls[0]["results"] == []
    row = writers.get_timesheet_run(s["db"], params["run_id"])
    assert row["ok_count"] == 2


def test_transient_analyzer_error_leaves_pending(ctx_and_fakes):
    s = ctx_and_fakes
    s["analyzer"].raise_exc = TransientError("rate limited")
    params = _params(s["db"])

    with pytest.raises(TransientError):
        run_timesheet_review(params)

    assert writers.get_timesheet_run(s["db"], params["run_id"])["status"] == "pending"
    assert s["odoo"].calls == []


def test_permanent_analyzer_error_marks_failed(ctx_and_fakes):
    s = ctx_and_fakes
    s["analyzer"].raise_exc = PermanentError("invalid tool")
    params = _params(s["db"])

    with pytest.raises(PermanentError):
        run_timesheet_review(params)

    row = writers.get_timesheet_run(s["db"], params["run_id"])
    assert row["status"] == "failed"
    assert "invalid tool" in row["error_message"]
    assert s["odoo"].calls == []


def test_instance_budget_gate_declines_before_paid_call(ctx_and_fakes, monkeypatch):
    s = ctx_and_fakes
    monkeypatch.setattr(
        "worker.timesheet_runner.instance_budget_exceeded", lambda ctx, iid: 10.0
    )
    params = _params(s["db"])

    with pytest.raises(PermanentError):
        run_timesheet_review(params)

    row = writers.get_timesheet_run(s["db"], params["run_id"])
    assert row["status"] == "failed"
    assert "budget" in row["error_message"].lower()
    assert s["analyzer"].calls == []
