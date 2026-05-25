"""Tests for ticket_runner.run_ticket_analysis.

Real SQLite DB so writer + idempotency paths are exercised against SQL.
Fakes for TicketAnalyzer and OdooCallbackClient.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.errors import PermanentError, TransientError
from reva.ticket_analyzer import format_ticket_html
from reva.types import (
    AcceptanceCriterion,
    ClaudeResponse,
    TicketAnalysisResult,
    TicketJobParams,
    TicketTestCase,
)
from worker.runner import WorkerContext, set_context
from worker.ticket_runner import run_ticket_analysis


# --- Fakes -------------------------------------------------------------------


@dataclass
class FakeTicketAnalyzer:
    result: TicketAnalysisResult | None = None
    raise_exc: Exception | None = None
    call_count: int = 0

    def analyze_with_response(
        self, params: TicketJobParams
    ) -> tuple[ClaudeResponse, TicketAnalysisResult]:
        self.call_count += 1
        if self.raise_exc:
            raise self.raise_exc
        assert self.result is not None
        response = ClaudeResponse(
            model="claude-sonnet-4-6",
            stop_reason="tool_use",
            content=[],
            input_tokens=1000,
            output_tokens=300,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            tool_use_name=None,
            tool_use_input=None,
        )
        return response, self.result


@dataclass
class FakeOdoo:
    raise_exc: Exception | None = None
    call_count: int = 0
    calls: list[dict] = field(default_factory=list)

    def write_field(self, ticket_id, model_name, field_name, html):
        self.call_count += 1
        self.calls.append(
            {"ticket_id": ticket_id, "model_name": model_name,
             "field_name": field_name, "html": html}
        )
        if self.raise_exc:
            raise self.raise_exc


# --- Helpers -----------------------------------------------------------------


def _good_result() -> TicketAnalysisResult:
    return TicketAnalysisResult(
        summary="The ticket is well-written.",
        missing_info=["User role unspecified"],
        acceptance_criteria=[
            AcceptanceCriterion(given="user", when="clicks", then="action fires")
        ],
        test_cases=[
            TicketTestCase(category="happy_path", description="Standard click"),
        ],
        definition_of_ready=["Scope clear"],
        definition_of_done=["Code reviewed"],
        odoo_notes=["Affects helpdesk.ticket"],
    )


@pytest.fixture()
def ctx_and_fakes():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)

    analyzer = FakeTicketAnalyzer(result=_good_result())
    odoo = FakeOdoo()
    ctx = WorkerContext(
        db=db,
        claude=None,  # type: ignore[arg-type]
        github=None,  # type: ignore[arg-type]
        reviewer=None,  # type: ignore[arg-type]
        ticket_analyzer=analyzer,  # type: ignore[arg-type]
        odoo=odoo,  # type: ignore[arg-type]
    )
    set_context(ctx)
    return {"ctx": ctx, "db": db, "analyzer": analyzer, "odoo": odoo}


def _make_params(db: Database) -> dict:
    params = TicketJobParams(
        analysis_id=0,
        ticket_id=42,
        model_name="helpdesk.ticket",
        field_name="description",
        text="Add a button to the form view.",
    )
    analysis_id = writers.record_ticket_analysis_created(db, params)
    writers.attach_ticket_job_id(db, analysis_id, "rq:job:test-123")
    return TicketJobParams(
        analysis_id=analysis_id,
        ticket_id=params.ticket_id,
        model_name=params.model_name,
        field_name=params.field_name,
        text=params.text,
    ).model_dump()


# --- Tests -------------------------------------------------------------------


def test_happy_path(ctx_and_fakes):
    s = ctx_and_fakes
    params = _make_params(s["db"])

    out = run_ticket_analysis(params)

    assert out["status"] == "completed"
    assert s["analyzer"].call_count == 1
    assert s["odoo"].call_count == 1

    row = writers.get_ticket_analysis(s["db"], out["analysis_id"])
    assert row is not None
    assert row["status"] == "completed"
    assert row["result_html"] is not None
    assert "<h2>Summary</h2>" in row["result_html"]
    assert row["estimated_cost_usd"] is not None and row["estimated_cost_usd"] > 0


def test_html_stored_before_odoo_call(ctx_and_fakes):
    s = ctx_and_fakes
    s["odoo"].raise_exc = PermanentError("Odoo 404")
    params = _make_params(s["db"])

    with pytest.raises(PermanentError):
        run_ticket_analysis(params)

    # DB must have result_html despite Odoo failure
    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["result_html"] is not None
    assert row["status"] == "completed"


def test_transient_error_from_analyzer(ctx_and_fakes):
    s = ctx_and_fakes
    s["analyzer"].raise_exc = TransientError("rate limited")
    params = _make_params(s["db"])

    with pytest.raises(TransientError):
        run_ticket_analysis(params)

    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["status"] == "pending"
    assert s["odoo"].call_count == 0


def test_permanent_error_from_analyzer(ctx_and_fakes):
    s = ctx_and_fakes
    s["analyzer"].raise_exc = PermanentError("Invalid tool call")
    params = _make_params(s["db"])

    with pytest.raises(PermanentError):
        run_ticket_analysis(params)

    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["status"] == "failed"
    assert s["odoo"].call_count == 0


def test_transient_error_from_odoo(ctx_and_fakes):
    s = ctx_and_fakes
    s["odoo"].raise_exc = TransientError("Odoo 503")
    params = _make_params(s["db"])

    with pytest.raises(TransientError):
        run_ticket_analysis(params)

    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["status"] == "completed"
    assert row["result_html"] is not None


def test_permanent_error_from_odoo(ctx_and_fakes):
    s = ctx_and_fakes
    s["odoo"].raise_exc = PermanentError("Odoo 401")
    params = _make_params(s["db"])

    with pytest.raises(PermanentError):
        run_ticket_analysis(params)

    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["status"] == "completed"
    assert row["result_html"] is not None


def test_dedup_pending(ctx_and_fakes):
    s = ctx_and_fakes
    params = _make_params(s["db"])

    existing = writers.get_pending_ticket_analysis(
        s["db"],
        ticket_id=42,
        model_name="helpdesk.ticket",
        field_name="description",
    )
    assert existing is not None
    assert existing["id"] == params["analysis_id"]
    assert existing["status"] == "pending"
