"""PR closing-reference to Odoo-ticket resolution."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url
from reva.db.models import TicketIssueRun
from reva.ticket_links import parse_closing_refs, resolve_pr_tickets


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _issue_run(
    ticket_id: int,
    repo_full_name: str,
    issues: list[dict],
    *,
    odoo_instance_id: int = 1,
) -> TicketIssueRun:
    return TicketIssueRun(
        ticket_id=ticket_id,
        model_name="helpdesk.ticket",
        odoo_instance_id=odoo_instance_id,
        github_url=f"https://github.com/{repo_full_name}",
        repo_full_name=repo_full_name,
        name=f"Ticket {ticket_id}",
        description="ticket",
        analysis_html="<p>analysis</p>",
        priority="1",
        ticket_url=f"https://odoo.example/tickets/{ticket_id}",
        status="completed",
        issues=issues,
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )


def test_parse_closing_refs_dedups_same_repo_refs() -> None:
    body = "Closes #10, fixes #11, resolved #10, see owner/repo#12"

    assert parse_closing_refs(body) == [10, 11]


def test_resolve_pr_tickets_matches_repo_and_dedups_ticket(db: Database) -> None:
    with db.session() as s:
        s.add(_issue_run(123, "acme/widgets", [{"number": 10}, {"number": 11}]))
        s.add(_issue_run(123, "acme/widgets", [{"number": 12}]))
        s.add(_issue_run(456, "other/widgets", [{"number": 10}]))

    refs = resolve_pr_tickets(db, "ACME/Widgets", [10, 12])

    assert [(r.odoo_instance_id, r.ticket_id, r.model_name) for r in refs] == [
        (1, 123, "helpdesk.ticket")
    ]

