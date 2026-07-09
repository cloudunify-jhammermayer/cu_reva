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


def test_project_fields_default_none_and_round_trip():
    db = _db()
    run_id = writers.record_ticket_issue_run_created(db, _params())
    row = writers.get_ticket_issue_run(db, run_id)
    assert row["github_project_url"] is None
    assert row["plan_date"] is None

    from datetime import date
    p2 = _params().model_copy(update={
        "ticket_id": 456,
        "github_project_url": "https://github.com/orgs/acme/projects/5",
        "plan_date": date(2026, 7, 15),
    })
    run_id = writers.record_ticket_issue_run_created(db, p2)
    row = writers.get_ticket_issue_run(db, run_id)
    assert row["github_project_url"] == "https://github.com/orgs/acme/projects/5"
    assert row["plan_date"] == date(2026, 7, 15)


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


def _complete_run(db, params, issues):
    run_id = writers.record_ticket_issue_run_created(db, params)
    writers.update_ticket_issue_progress(db, run_id, issues)
    writers.record_ticket_issue_run_completed(db, run_id, issues)
    return run_id


def test_union_dedups_newest_wins_and_scopes_by_instance(db):
    p = _typed_params(ticket_id=90)
    _complete_run(db, p, [
        {"number": 1, "title": "old title", "url": "https://gh/1", "state": "closed"},
        {"number": 2, "title": "two", "url": "https://gh/2", "state": "open"},
        {"number": None, "title": "never created", "url": None, "state": None},
    ])
    _complete_run(db, p, [
        {"number": 1, "title": "new title", "url": "https://gh/1", "state": "open"},
        {"number": 3, "title": "three", "url": "https://gh/3", "state": "open"},
    ])
    # same ticket id on ANOTHER instance must not leak in
    _complete_run(db, _typed_params(ticket_id=90, odoo_instance_id=2),
                  [{"number": 99, "title": "other", "url": "https://gh/99", "state": "open"}])

    union = writers.get_ticket_issue_union(db, 1, 90, "helpdesk.ticket")
    assert [i["number"] for i in union] == [1, 2, 3]
    assert union[0]["title"] == "new title"      # newest run wins
    assert union[1]["state"] == "open"


def test_ready_tickets_require_nonempty_all_closed_union(db):
    _complete_run(db, _typed_params(ticket_id=92), [
        {"number": 10, "title": "A", "url": "https://gh/10", "state": "closed"},
        {"number": 11, "title": "B", "url": "https://gh/11", "state": "closed"},
    ])
    _complete_run(db, _typed_params(ticket_id=93), [
        {"number": 12, "title": "C", "url": "https://gh/12", "state": "closed"},
        {"number": 13, "title": "D", "url": "https://gh/13", "state": "open"},
    ])

    ready = writers.list_ready_tickets(db)

    assert writers.count_ready_tickets(db) == 1
    assert [(row["ticket_id"], row["issue_count"]) for row in ready] == [(92, 2)]


def test_update_state_stamps_complete_date(db):
    run_id = _complete_run(db, _typed_params(ticket_id=94), [
        {"number": 20, "title": "A", "url": "https://gh/20", "state": "open"},
    ])
    writers.update_ticket_issue_state(db, "org", "repo", 20, "closed",
                                      closed_at="2026-07-09T14:03:22Z")
    item = writers.get_ticket_issue_run(db, run_id)["issues"][0]
    assert item["complete_date"] == "2026-07-09"
    # reopen clears it
    writers.update_ticket_issue_state(db, "org", "repo", 20, "open", closed_at=None)
    item = writers.get_ticket_issue_run(db, run_id)["issues"][0]
    assert item["complete_date"] is None


def test_union_carries_dates(db):
    _complete_run(db, _typed_params(ticket_id=95), [
        {"number": 30, "title": "A", "url": "https://gh/30", "state": "closed",
         "plan_date": "2026-07-15", "complete_date": "2026-07-09"},
        {"number": 31, "title": "B", "url": "https://gh/31", "state": "open"},
    ])
    union = writers.get_ticket_issue_union(db, 1, 95, "helpdesk.ticket")
    assert union[0]["plan_date"] == "2026-07-15"
    assert union[0]["complete_date"] == "2026-07-09"
    assert union[1]["plan_date"] is None
    assert union[1]["complete_date"] is None


def test_latest_parent_scoped_and_excludes_self(db):
    p = _typed_params(ticket_id=91)
    r1 = _complete_run(db, p, [{"number": 5, "title": "t", "url": "https://gh/5", "state": "open"}])
    parent = {"number": 4, "id": 900004, "url": "https://gh/4", "title": "[DEV] 91 - Epic", "state": "open"}
    writers.set_ticket_issue_parent(db, r1, parent)

    got = writers.get_latest_ticket_issue_parent(
        db, 1, 91, "helpdesk.ticket", "org/repo", exclude_run_id=999)
    assert got == parent
    # own run excluded; other repo/instance → None
    assert writers.get_latest_ticket_issue_parent(db, 1, 91, "helpdesk.ticket", "org/repo", exclude_run_id=r1) is None
    assert writers.get_latest_ticket_issue_parent(db, 1, 91, "helpdesk.ticket", "org/other", exclude_run_id=999) is None
    assert writers.get_latest_ticket_issue_parent(db, 2, 91, "helpdesk.ticket", "org/repo", exclude_run_id=999) is None
