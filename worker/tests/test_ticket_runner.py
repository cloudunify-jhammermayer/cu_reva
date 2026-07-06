"""Tests for ticket_runner.run_ticket_analysis.

Real SQLite DB so writer + idempotency paths are exercised against SQL.
Fakes for TicketAnalyzer and OdooCallbackClient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import OpsEvent
from reva.errors import MalformedModelOutput, PermanentError, TransientError
from reva.types import (
    AcceptanceCriterion,
    ClaudeResponse,
    MissingInfoItem,
    SourcedItem,
    TicketAnalysisResult,
    TicketJobParams,
    TicketTestCase,
)
from worker.runner import WorkerContext, set_context
from worker.ticket_runner import run_ticket_analysis

_PROMPTS_DIR = str(Path(__file__).resolve().parents[2] / "prompts")


# --- Fakes -------------------------------------------------------------------


@dataclass
class FakeTicketAnalyzer:
    result: TicketAnalysisResult | None = None
    raise_exc: Exception | None = None
    raise_once: Exception | None = None  # raised on the first call only
    call_count: int = 0
    extra_blocks: list | None = None

    def analyze_with_response(
        self,
        params: TicketJobParams,
        extra_system_blocks: list | None = None,
    ) -> tuple[ClaudeResponse, TicketAnalysisResult]:
        self.call_count += 1
        self.extra_blocks = extra_system_blocks
        if self.raise_once:
            exc, self.raise_once = self.raise_once, None
            raise exc
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
        missing_info=[MissingInfoItem(text="User role unspecified")],
        acceptance_criteria=[
            AcceptanceCriterion(given="user", when="clicks", then="action fires")
        ],
        test_cases=[
            TicketTestCase(category="happy_path", description="Standard click"),
        ],
        definition_of_ready=[SourcedItem(text="Scope clear")],
        definition_of_done=[SourcedItem(text="Code reviewed")],
        odoo_notes=[SourcedItem(text="Affects helpdesk.ticket")],
    )


@pytest.fixture()
def ctx_and_fakes(monkeypatch):
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)

    analyzer = FakeTicketAnalyzer(result=_good_result())
    odoo = FakeOdoo()
    ctx = WorkerContext(
        db=db,
        claude=None,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        github=None,  # type: ignore[arg-type]
        reviewer=None,  # type: ignore[arg-type]
        auditor=None,  # type: ignore[arg-type]
        ticket_analyzer=analyzer,  # type: ignore[arg-type]
        verifier=None,  # type: ignore[arg-type] — unused in ticket tests
    )
    monkeypatch.setattr("worker.ticket_runner.build_odoo_client", lambda ctx, _id: odoo)
    set_context(ctx)
    return {"ctx": ctx, "db": db, "analyzer": analyzer, "odoo": odoo}


def _make_params(db: Database) -> dict:
    params = TicketJobParams(
        analysis_id=0,
        odoo_instance_id=1,
        ticket_id=42,
        model_name="helpdesk.ticket",
        field_name="description",
        text="Add a button to the form view.",
    )
    analysis_id = writers.record_ticket_analysis_created(db, params)
    writers.attach_ticket_job_id(db, analysis_id, "rq:job:test-123")
    return TicketJobParams(
        analysis_id=analysis_id,
        odoo_instance_id=1,
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


def test_malformed_output_retried_once_then_completes(ctx_and_fakes):
    s = ctx_and_fakes
    s["analyzer"].raise_once = MalformedModelOutput("truncated at max_tokens=16384")
    params = _make_params(s["db"])

    out = run_ticket_analysis(params)

    assert out["status"] == "completed"
    assert s["analyzer"].call_count == 2
    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["status"] == "completed"
    # The absorbed first attempt must be visible as an ops event.
    with s["db"].session() as session:
        event = session.query(OpsEvent).filter_by(event="malformed_output_retried").one()
        assert event.severity == "warning"


def test_malformed_output_twice_fails_permanently(ctx_and_fakes):
    s = ctx_and_fakes
    s["analyzer"].raise_exc = MalformedModelOutput("schema validation: summary missing")
    params = _make_params(s["db"])

    with pytest.raises(PermanentError):
        run_ticket_analysis(params)

    assert s["analyzer"].call_count == 2
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


def test_retry_after_callback_failure_does_not_reanalyze(ctx_and_fakes):
    """H7: once the result is persisted, an RQ retry (the row is already
    completed) reuses the HTML and retries only the Odoo callback — it must never
    re-run the paid analyzer. This is what makes adding retry= safe."""
    s = ctx_and_fakes
    params = _make_params(s["db"])

    # First attempt: analysis persists, then the Odoo callback fails transiently.
    s["odoo"].raise_exc = TransientError("Odoo 503")
    with pytest.raises(TransientError):
        run_ticket_analysis(params)
    assert s["analyzer"].call_count == 1
    assert writers.get_ticket_analysis(s["db"], params["analysis_id"])["status"] == "completed"

    # RQ retry: callback succeeds now. The analyzer must NOT be invoked again.
    s["odoo"].raise_exc = None
    out = run_ticket_analysis(params)
    assert out["status"] == "completed"
    assert s["analyzer"].call_count == 1  # not re-analyzed (no re-pay)
    assert s["odoo"].call_count == 2      # callback was retried


def test_dedup_pending(ctx_and_fakes):
    s = ctx_and_fakes
    params = _make_params(s["db"])

    existing = writers.get_pending_ticket_analysis(
        s["db"],
        ticket_id=42,
        model_name="helpdesk.ticket",
        field_name="description",
        odoo_instance_id=1,
    )
    assert existing is not None
    assert existing["id"] == params["analysis_id"]
    assert existing["status"] == "pending"


def test_instance_budget_gate_declines_before_paid_call(ctx_and_fakes, monkeypatch):
    """An over-budget instance's queued job fails fast: no paid analyzer call."""
    s = ctx_and_fakes
    monkeypatch.setattr(
        "worker.ticket_runner.instance_budget_exceeded", lambda ctx, iid: 12.5
    )
    params = _make_params(s["db"])

    with pytest.raises(PermanentError):
        run_ticket_analysis(params)

    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["status"] == "failed"
    assert "budget" in row["error_message"].lower()
    assert s["analyzer"].call_count == 0


def test_knowledge_block_passed_and_spend_recorded(ctx_and_fakes, monkeypatch):
    s = ctx_and_fakes
    block = {
        "type": "text",
        "text": "Retrieved Odoo knowledge ...",
        "cache_control": {"type": "ephemeral"},
    }
    monkeypatch.setattr(
        "worker.ticket_runner.build_knowledge_block",
        lambda claude, core, prompts, version, text: (block, 0.002, None),
    )
    monkeypatch.setattr(
        "worker.ticket_runner.instance_odoo_version",
        lambda ctx, iid: "19.0",
    )
    fake_core = type("CK", (), {"resolve": lambda self, version: "19.0"})()
    object.__setattr__(s["ctx"], "core_knowledge", fake_core)
    object.__setattr__(s["ctx"], "prompts_dir", _PROMPTS_DIR)
    params = _make_params(s["db"])

    out = run_ticket_analysis(params)

    assert out["status"] == "completed"
    assert s["analyzer"].extra_blocks == [block]

    from datetime import datetime, timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    assert writers.sum_estimated_cost_since(s["db"], since) >= 0.002
