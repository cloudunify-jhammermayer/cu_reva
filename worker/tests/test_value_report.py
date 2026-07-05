"""Monthly value-report rollups and persistence."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import (
    ClaudeSpend,
    OdooInstance,
    PullRequest,
    Repository,
    ReviewFinding,
    ReviewRun,
    TicketAnalysis,
    TicketIssueRun,
)
from reva.value_report import build_report

_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
_END = datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _seed_report_data(db: Database) -> None:
    with db.session() as s:
        repo = Repository(
            github_repository_id=1001,
            owner="acme",
            name="widgets",
            full_name="acme/widgets",
            installation_id=555,
        )
        s.add(repo)
        s.flush()
        pr = PullRequest(
            repository_id=repo.id,
            github_pr_id=2001,
            pr_number=42,
            title="Fix login",
            base_branch="main",
            head_branch="fix-login",
            head_sha="abc",
            state="open",
        )
        s.add(pr)
        s.flush()
        run = ReviewRun(
            repository_id=repo.id,
            pull_request_id=pr.id,
            head_sha="abc",
            status="completed",
            trigger_event="pull_request",
            review_mode="diff",
            estimated_cost_usd=1.25,
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        s.add(run)
        s.flush()
        s.add(ReviewFinding(
            review_run_id=run.id,
            severity="high",
            category="bug",
            title="Broken auth",
            body="Details",
            outcome="resolved_by_fix",
        ))
        inst = OdooInstance(name="Prod", key_hash="hash", key_prefix="rev_prod")
        s.add(inst)
        s.flush()
        s.add(TicketAnalysis(
            ticket_id=10,
            model_name="helpdesk.ticket",
            field_name="description",
            odoo_instance_id=inst.id,
            input_text="ticket",
            status="completed",
            created_at=datetime(2026, 6, 8, tzinfo=timezone.utc),
        ))
        s.add(TicketAnalysis(
            ticket_id=11,
            model_name="helpdesk.ticket",
            field_name="description",
            odoo_instance_id=inst.id,
            input_text="old ticket",
            status="completed",
            created_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
        ))
        s.add(TicketIssueRun(
            ticket_id=10,
            model_name="helpdesk.ticket",
            odoo_instance_id=inst.id,
            github_url="https://github.com/acme/widgets",
            repo_full_name="acme/widgets",
            name="Fix login",
            description="ticket",
            analysis_html="<p>analysis</p>",
            priority="1",
            ticket_url="https://odoo.example/tickets/10",
            status="completed",
            created_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
        ))
        s.add(ClaudeSpend(
            kind="review",
            cost_usd=1.25,
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        ))


def test_build_report_counts_period_data(db: Database) -> None:
    _seed_report_data(db)

    content, stats = build_report(db, _START, _END)

    assert stats == {
        "reviews": 1,
        "findings": 1,
        "resolved_by_fix": 1,
        "dismissed": 0,
        "spend_usd": 1.25,
    }
    assert "| acme/widgets | 1 | $1.25 |" in content
    assert "| Prod | 1 | 1 |" in content


def test_value_report_upsert_replaces_and_resets_chat_sent(db: Database) -> None:
    report_id = writers.upsert_value_report(db, _START, _END, "v1", {"reviews": 0})
    writers.set_value_report_chat_sent(db, report_id)

    report_id_2 = writers.upsert_value_report(db, _START, _END, "v2", {"reviews": 1})
    rows = writers.get_value_reports(db)

    assert report_id_2 == report_id
    assert len(rows) == 1
    assert rows[0]["content_md"] == "v2"
    assert rows[0]["stats"] == {"reviews": 1}
    assert rows[0]["chat_sent"] is False

