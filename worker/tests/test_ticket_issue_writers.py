"""Writer-level tests for the parent_issue column (real SQLite)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.types import TicketIssueJobParams


def _db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


@pytest.fixture
def db() -> Database:
    return _db()


def _params() -> TicketIssueJobParams:
    return TicketIssueJobParams(
        run_id=0, odoo_instance_id=1, ticket_id=123, model_name="helpdesk.ticket",
        github_url="https://github.com/acme/widgets", name="n", description="d",
        analysis_html="a", priority="1", ticket_url="https://odoo.example/web#id=123",
    )


def _typed_params(**overrides) -> TicketIssueJobParams:
    base = dict(
        run_id=0, odoo_instance_id=1, ticket_id=77, model_name="helpdesk.ticket",
        github_url="https://github.com/org/repo", name="Ticket name",
        description="Change the delivery slip layout", analysis_html="",
        priority="1", ticket_url="https://odoo.example.com/web#id=77",
    )
    base.update(overrides)
    return TicketIssueJobParams(**base)


def test_parent_issue_defaults_to_none_and_round_trips():
    db = _db()
    run_id = writers.record_ticket_issue_run_created(db, _params())
    assert writers.get_ticket_issue_run(db, run_id)["parent_issue"] is None

    parent = {"number": 10, "id": 900, "url": "https://github.com/acme/widgets/issues/10",
              "title": "[Ticket 123] n", "state": "open"}
    writers.set_ticket_issue_parent(db, run_id, parent)
    assert writers.get_ticket_issue_run(db, run_id)["parent_issue"] == parent


def test_purge_preserves_parent_issue():
    db = _db()
    run_id = writers.record_ticket_issue_run_created(db, _params())
    writers.set_ticket_issue_parent(db, run_id, {"number": 10, "id": 900,
        "url": "https://github.com/acme/widgets/issues/10", "title": "[Ticket 123] n", "state": "open"})
    writers.update_ticket_issue_progress(db, run_id, [
        {"title": "c", "number": 11, "url": "u", "state": "open", "id": 901,
         "attached": True, "body": "secret", "acceptance_criteria": ["x"]},
    ])
    # backdate so the purge cutoff catches it
    from reva.db.models import TicketIssueRun
    with db.session() as s:
        s.get(TicketIssueRun, run_id).created_at = datetime.now(timezone.utc) - timedelta(days=40)

    writers.purge_old_ticket_issue_text(db, older_than_days=30)

    row = writers.get_ticket_issue_run(db, run_id)
    assert row["parent_issue"]["number"] == 10          # parent untouched
    assert "body" not in row["issues"][0]               # child text scrubbed
    assert row["issues"][0]["attached"] is True          # resume metadata kept


def test_planning_basis_typed_prefix():
    untyped = writers.compute_planning_basis(_typed_params())
    typed = writers.compute_planning_basis(_typed_params(issue_type="CR"))
    assert untyped.startswith("text:")          # pre-type format unchanged
    assert typed == "cr:" + untyped             # type folded in front


def test_issue_type_persisted_roundtrip(db):
    run_id = writers.record_ticket_issue_run_created(db, _typed_params(issue_type="CR"))
    assert writers.get_ticket_issue_run(db, run_id)["issue_type"] == "CR"
    # untyped stays NULL (different ticket: one pending run per record)
    run_id2 = writers.record_ticket_issue_run_created(db, _typed_params(ticket_id=78))
    assert writers.get_ticket_issue_run(db, run_id2)["issue_type"] is None
