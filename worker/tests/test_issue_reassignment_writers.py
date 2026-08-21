"""Writer-level tests for the issue-ownership override table (real SQLite)."""
from __future__ import annotations

import pytest

from reva.db import Base, Database, create_engine_from_url, writers


@pytest.fixture
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _instance(db: Database, name: str = "acme") -> int:
    # key_hash/key_prefix are required keyword args — see the convention in
    # worker/tests/test_odoo_instance_writers.py.
    return writers.create_odoo_instance(
        db, name=name, key_hash=f"h-{name}", key_prefix=f"reva_odoo_{name[:2]}",
        callback_url="", callback_api_key_enc="enc",
    )


def test_records_and_reads_back_an_override(db):
    iid = _instance(db)
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    assert writers.issue_owner_overrides(db, iid, "acme/widgets", [42]) == {
        42: (5678, "helpdesk.ticket")
    }


def test_repo_name_is_matched_case_insensitively(db):
    # ticket_issue_runs.repo_full_name is stored lowercased; a caller passing
    # GitHub's original casing must still match.
    iid = _instance(db)
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="Acme/Widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    assert writers.issue_owner_overrides(db, iid, "acme/widgets", [42]) == {
        42: (5678, "helpdesk.ticket")
    }


def test_recording_twice_updates_rather_than_duplicating(db):
    iid = _instance(db)
    for ticket_id in (5678, 9999):
        writers.record_issue_reassignment(
            db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
            ticket_id=ticket_id, model_name="project.task",
        )
    assert writers.issue_owner_overrides(db, iid, "acme/widgets", [42]) == {
        42: (9999, "project.task")
    }


def test_clear_removes_the_override(db):
    iid = _instance(db)
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    writers.clear_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
    )
    assert writers.issue_owner_overrides(db, iid, "acme/widgets", [42]) == {}


def test_clear_is_a_noop_when_there_is_nothing_to_clear(db):
    iid = _instance(db)
    writers.clear_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
    )  # no raise


def test_overrides_are_scoped_to_the_instance(db):
    one, two = _instance(db), _instance(db, name="other")
    writers.record_issue_reassignment(
        db, odoo_instance_id=one, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    assert writers.issue_owner_overrides(db, two, "acme/widgets", [42]) == {}


def test_legacy_null_instance_resolves_no_overrides(db):
    # Pre-multi-instance runs carry a NULL odoo_instance_id. They can never
    # have an override (the endpoint is instance-gated), and passing None must
    # not match every row.
    iid = _instance(db)
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    assert writers.issue_owner_overrides(db, None, "acme/widgets", [42]) == {}
    assert writers.issues_moved_onto(db, None, 5678, "helpdesk.ticket") == []


def test_issues_moved_onto_lists_the_target_side(db):
    iid = _instance(db)
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=43,
        ticket_id=1234, model_name="project.task",
    )
    assert writers.issues_moved_onto(db, iid, 5678, "helpdesk.ticket") == [
        ("acme/widgets", 42)
    ]


def _run_with_issues(db: Database, instance_id: int, ticket_id: int,
                     model_name: str, numbers: list[int]) -> int:
    """A completed create-issues run owning `numbers` on acme/widgets."""
    from reva.types import TicketIssueJobParams

    run_id = writers.record_ticket_issue_run_created(db, TicketIssueJobParams(
        run_id=0, odoo_instance_id=instance_id, ticket_id=ticket_id,
        model_name=model_name, github_url="https://github.com/acme/widgets",
        name="Ticket name", description="d", analysis_html="",
        priority="1", ticket_url="https://odoo.example/web#id=1",
    ))
    writers.update_ticket_issue_progress(db, run_id, [
        {"title": f"Issue {n}", "number": n,
         "url": f"https://github.com/acme/widgets/issues/{n}", "state": "open"}
        for n in numbers
    ])
    return run_id


def test_union_drops_an_issue_moved_away(db):
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42, 43])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    union = writers.get_ticket_issue_union(db, iid, 1234, "project.task")
    assert [i["number"] for i in union] == [43]


def test_union_adds_an_issue_moved_on(db):
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42, 43])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    union = writers.get_ticket_issue_union(db, iid, 5678, "helpdesk.ticket")
    assert [i["number"] for i in union] == [42]
    # The item travels intact — Odoo re-renders its links from this payload.
    assert union[0]["title"] == "Issue 42"
    assert union[0]["url"] == "https://github.com/acme/widgets/issues/42"
    assert union[0]["state"] == "open"


def test_union_for_a_target_with_no_runs_at_all(db):
    """The case a naive implementation drops: the record the issue moved onto
    has never had a create-issues run, so there is nothing to read it off."""
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    union = writers.get_ticket_issue_union(db, iid, 5678, "helpdesk.ticket")
    assert [i["number"] for i in union] == [42]


def test_union_reflects_state_written_after_the_move(db):
    """State sync writes into the SOURCE's run row, because that is where the
    issue plan still lives. The target's union must show it."""
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    writers.update_ticket_issue_state(db, "acme", "widgets", 42, "closed",
                                      "2026-08-20T10:00:00Z")
    union = writers.get_ticket_issue_union(db, iid, 5678, "helpdesk.ticket")
    assert union[0]["state"] == "closed"
    assert union[0]["complete_date"] == "2026-08-20"


def test_union_is_unchanged_when_nothing_was_moved(db):
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42, 43])
    union = writers.get_ticket_issue_union(db, iid, 1234, "project.task")
    assert [i["number"] for i in union] == [42, 43]
