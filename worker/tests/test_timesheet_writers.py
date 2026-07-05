"""Writer tests for timesheet_review_runs / timesheet_review_lines."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import ClaudeSpend
from reva.types import ClaudeResponse, TimesheetJobParams, TimesheetLine, TimesheetLineResult


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _line(line_id: int, desc: str = "fixed stupid bug") -> TimesheetLine:
    return TimesheetLine(
        line_id=line_id,
        task_name="Reports",
        project_name="ACME Rollout",
        user_name="Jo Dev",
        user_role="developer",
        description=desc,
    )


def _params(n: int = 3) -> TimesheetJobParams:
    return TimesheetJobParams(
        run_id=0,
        odoo_instance_id=1,
        request_id="req-1",
        flagged_words=["stupid"],
        lines=[_line(i) for i in range(1, n + 1)],
    )


def _response(cost_tokens: int = 1000) -> ClaudeResponse:
    return ClaudeResponse(
        model="claude-sonnet-4-6",
        stop_reason="tool_use",
        tool_use_input=None,
        input_tokens=cost_tokens,
        output_tokens=200,
        cache_read_tokens=0,
        cache_creation_tokens=0,
    )


def test_create_sets_pending_and_total(db):
    run_id = writers.record_timesheet_run_created(db, _params())
    run = writers.get_timesheet_run(db, run_id)
    assert run["status"] == "pending"
    assert run["total_lines"] == 3
    assert run["callback_payload"] is None
    assert run["callback_sent_at"] is None


def test_pending_lookup_and_failed_clears_it(db):
    run_id = writers.record_timesheet_run_created(db, _params())
    found = writers.get_pending_timesheet_run(db, 1, "req-1")
    assert found is not None and found["id"] == run_id and found["created_at"] is not None
    assert writers.get_pending_timesheet_run(db, 1, "other") is None
    assert writers.get_pending_timesheet_run(db, 2, "req-1") is None
    writers.record_timesheet_run_failed(db, run_id, "boom")
    assert writers.get_pending_timesheet_run(db, 1, "req-1") is None
    assert writers.get_timesheet_run(db, run_id)["error_message"] == "boom"


def test_attach_job_id(db):
    run_id = writers.record_timesheet_run_created(db, _params())
    writers.attach_timesheet_job_id(db, run_id, "rq:job:1")
    assert writers.get_timesheet_run(db, run_id)["job_id"] == "rq:job:1"


def test_chunk_persists_lines_payload_tokens_and_spend(db):
    run_id = writers.record_timesheet_run_created(db, _params())
    writers.record_timesheet_chunk(
        db,
        run_id,
        [
            TimesheetLineResult(line_id=1, status="ok"),
            TimesheetLineResult(
                line_id=2,
                status="rewritten",
                updated_desc="Implemented reports",
            ),
        ],
        [_response(1000)],
    )
    writers.record_timesheet_chunk(
        db,
        run_id,
        [TimesheetLineResult(line_id=3, status="needs_human", reason="zu unkonkret")],
        [_response(500), _response(300)],
    )
    assert writers.get_timesheet_line_ids(db, run_id) == {1, 2, 3}
    run = writers.get_timesheet_run(db, run_id)
    assert run["input_tokens"] == 1800
    assert run["estimated_cost_usd"] > 0
    assert run["callback_payload"]["results"] == [
        {"line_id": 2, "status": "rewritten", "updated_desc": "Implemented reports"},
        {"line_id": 3, "status": "needs_human", "reason": "zu unkonkret"},
    ]
    with db.session() as s:
        kinds = s.execute(select(ClaudeSpend.kind)).scalars().all()
    assert kinds == ["timesheet_review", "timesheet_review", "timesheet_review"]


def test_completed_computes_counts(db):
    run_id = writers.record_timesheet_run_created(db, _params())
    writers.record_timesheet_chunk(
        db,
        run_id,
        [
            TimesheetLineResult(line_id=1, status="ok"),
            TimesheetLineResult(line_id=2, status="rewritten", updated_desc="x"),
            TimesheetLineResult(line_id=3, status="needs_human", reason="y"),
        ],
        [_response()],
    )
    writers.record_timesheet_run_completed(db, run_id)
    run = writers.get_timesheet_run(db, run_id)
    assert run["status"] == "completed"
    assert (run["ok_count"], run["rewritten_count"], run["needs_human_count"]) == (1, 1, 1)
    assert run["completed_at"] is not None


def test_callback_sent_clears_payload(db):
    run_id = writers.record_timesheet_run_created(db, _params())
    writers.record_timesheet_chunk(
        db,
        run_id,
        [TimesheetLineResult(line_id=1, status="rewritten", updated_desc="x")],
        [_response()],
    )
    writers.record_timesheet_run_completed(db, run_id)
    writers.record_timesheet_callback_sent(db, run_id)
    run = writers.get_timesheet_run(db, run_id)
    assert run["callback_payload"] is None
    assert run["callback_sent_at"] is not None
