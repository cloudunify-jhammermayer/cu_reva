"""Weekly report formatting."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.types import TicketIssueJobParams
from reva.weekly_report import build_weekly_report


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def test_weekly_report_includes_ready_tickets(db: Database) -> None:
    params = TicketIssueJobParams(
        run_id=0,
        odoo_instance_id=1,
        ticket_id=123,
        model_name="helpdesk.ticket",
        github_url="https://github.com/acme/widgets",
        name="Login work",
        description="d",
        analysis_html="a",
        priority="1",
        ticket_url="https://odoo.example/web#id=123",
    )
    issues = [
        {"number": 10, "title": "A", "url": "https://gh/10", "state": "closed"},
        {"number": 11, "title": "B", "url": "https://gh/11", "state": "closed"},
    ]
    run_id = writers.record_ticket_issue_run_created(db, params)
    writers.update_ticket_issue_progress(db, run_id, issues)
    writers.record_ticket_issue_run_completed(db, run_id, issues)

    report = build_weekly_report(
        db,
        since=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    assert "*Ready for deployment*" in report
    assert "`acme/widgets` ticket 123 (2 issues closed)" in report

