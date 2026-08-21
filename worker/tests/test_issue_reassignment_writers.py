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


def test_state_sync_notifies_the_target_not_the_source(db):
    """The whole point: the issue closes, and the record that hears about it is
    the one it was moved to."""
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    affected = writers.update_ticket_issue_state(
        db, "acme", "widgets", 42, "closed", "2026-08-20T10:00:00Z"
    )
    assert [(a["ticket_id"], a["model_name"]) for a in affected] == [
        (5678, "helpdesk.ticket")
    ]


def test_state_sync_still_writes_state_into_the_source_run(db):
    """State is a fact about the issue, and the plan still lives on the source's
    run — the write must not follow the notification."""
    iid = _instance(db)
    run_id = _run_with_issues(db, iid, 1234, "project.task", [42])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    writers.update_ticket_issue_state(db, "acme", "widgets", 42, "closed", None)
    stored = writers.get_ticket_issue_run(db, run_id)["issues"]
    assert stored[0]["state"] == "closed"


def test_state_sync_unaffected_without_an_override(db):
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42])
    affected = writers.update_ticket_issue_state(db, "acme", "widgets", 42, "closed", None)
    assert [(a["ticket_id"], a["model_name"]) for a in affected] == [
        (1234, "project.task")
    ]


def test_estimate_addressed_at_the_target_reaches_the_source_run(db):
    """Odoo sends the estimate from the record the issue now sits on, but the
    run holding the issue belongs to the record it came from."""
    iid = _instance(db)
    run_id = _run_with_issues(db, iid, 1234, "project.task", [42])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    target = writers.update_ticket_issue_estimate(
        db, iid, 5678, "helpdesk.ticket", 42, 3.5
    )
    assert target is not None
    stored = writers.get_ticket_issue_run(db, run_id)["issues"]
    assert stored[0]["estimate_hours"] == 3.5


def test_a_record_whose_only_issues_arrived_by_move_can_be_ready(db):
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    writers.update_ticket_issue_state(db, "acme", "widgets", 42, "closed", None)

    ready = writers.list_ready_tickets(db, limit=10)
    keys = {(t["ticket_id"], t["model_name"]) for t in ready}
    assert (5678, "helpdesk.ticket") in keys
    # The source has no issues left at all, so it is not "ready" — it is empty.
    assert (1234, "project.task") not in keys


def test_estimate_does_not_reach_another_instances_run_for_the_same_repo_and_number(db):
    """Fix round 1: natural_issue_owner (used to widen the owners search) must
    be instance-scoped like every other override consumer. Unscoped, a newer
    same-repo/same-number run belonging to a DIFFERENT instance could win the
    "which run is the natural source" lookup over instance A's own true
    source — losing the real write (owners no longer contains instance A's
    source) without ever reaching instance B's row (the final query is still
    instance-filtered), just silently dropping it on the floor."""
    iid_a, iid_b = _instance(db, "acme"), _instance(db, "other")
    run_a = _run_with_issues(db, iid_a, 1234, "project.task", [42])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid_a, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    # A different instance's run for the SAME repo/number, created after A's
    # true source — a naive unscoped "newest wins" lookup picks this instead.
    run_b = _run_with_issues(db, iid_b, 9999, "project.task", [42])

    target = writers.update_ticket_issue_estimate(
        db, iid_a, 5678, "helpdesk.ticket", 42, 3.5
    )

    assert target is not None
    stored_a = writers.get_ticket_issue_run(db, run_a)["issues"]
    assert stored_a[0]["estimate_hours"] == 3.5
    stored_b = writers.get_ticket_issue_run(db, run_b)["issues"]
    assert stored_b[0].get("estimate_hours") is None
