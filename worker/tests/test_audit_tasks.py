"""Tests for run_audit: spend recording + budget pre-check (SECU-4/CORR-11)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import AuditRun, ClaudeSpend
from reva.types import AuditResult
from worker.audit_tasks import run_audit
from worker.runner import WorkerContext, set_context


class FakeAuditor:
    def __init__(self, result: AuditResult):
        self.result = result
        self.called = False

    def execute(self, params):
        self.called = True
        return self.result


def _ctx(db, auditor, budget=None) -> WorkerContext:
    return WorkerContext(
        db=db, claude=None, runner=None, github=None,  # type: ignore[arg-type]
        reviewer=None, auditor=auditor, ticket_analyzer=None,  # type: ignore[arg-type]
        verifier=None, odoo=None, daily_budget_usd=budget,  # type: ignore[arg-type]
    )


@pytest.fixture()
def db():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    d = Database(engine)
    repo_id = writers.upsert_repository(
        d, github_repository_id=1, owner="acme", name="widgets",
        default_branch="main", installation_id=500,
    )
    return d, repo_id


def _result(cost: float) -> AuditResult:
    return AuditResult(status="completed", summary="ok", findings=[],
                       model="claude-opus-4-7", estimated_cost_usd=cost)


def _since():
    return datetime.now(timezone.utc) - timedelta(days=1)


def test_run_audit_records_spend_to_ledger(db):
    d, repo_id = db
    auditor = FakeAuditor(_result(cost=3.5))
    set_context(_ctx(d, auditor))

    out = run_audit({"repository_id": repo_id, "installation_id": 500})

    assert out["status"] == "completed"
    assert auditor.called
    assert writers.sum_estimated_cost_since(d, _since()) == pytest.approx(3.5)


def test_run_audit_declines_when_over_budget_without_running(db):
    """SECU-4: an audit is the most expensive path — it must respect the cap.
    A new audit is declined when over budget; no AuditRun row, auditor not run."""
    d, repo_id = db
    writers.record_claude_spend(d, "review", 50.0)  # already over the cap
    auditor = FakeAuditor(_result(cost=3.5))
    set_context(_ctx(d, auditor, budget=10.0))

    out = run_audit({"repository_id": repo_id, "installation_id": 500})

    assert out["status"] == "declined"
    assert auditor.called is False
    with d.session() as s:
        assert s.query(AuditRun).count() == 0
        # no new spend recorded for the declined audit
        assert s.query(ClaudeSpend).count() == 1
