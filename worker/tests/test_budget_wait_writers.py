from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.types import JobParams, TicketJobParams, TimesheetJobParams

SINCE = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _instance(db) -> int:
    return writers.create_odoo_instance(
        db, name="cu-prod", key_hash="h1", key_prefix="reva_odoo_aa",
        callback_url="", callback_api_key_enc="x",
    )


def test_set_and_clear_marker_on_ticket_analysis(db):
    inst = _instance(db)
    aid = writers.record_ticket_analysis_created(db, TicketJobParams(
        analysis_id=0, odoo_instance_id=inst, ticket_id=42, model_name="helpdesk.ticket",
        field_name="description", text="t"))
    assert writers.get_ticket_analysis(db, aid)["budget_wait_since"] is None

    writers.set_budget_wait_since(db, "ticket_analysis", aid, SINCE)
    row = writers.get_ticket_analysis(db, aid)
    assert row["status"] == "pending"
    assert row["budget_wait_since"].replace(tzinfo=timezone.utc) == SINCE

    writers.set_budget_wait_since(db, "ticket_analysis", aid, None)
    assert writers.get_ticket_analysis(db, aid)["budget_wait_since"] is None


def test_unknown_kind_is_a_programming_error(db):
    with pytest.raises(KeyError):
        writers.set_budget_wait_since(db, "nope", 1, SINCE)


def test_set_budget_wait_since_stores_and_clears_reason(db):
    inst = _instance(db)
    aid = writers.record_ticket_analysis_created(db, TicketJobParams(
        analysis_id=0, odoo_instance_id=inst, ticket_id=42, model_name="helpdesk.ticket",
        field_name="description", text="t"))

    writers.set_budget_wait_since(db, "ticket_analysis", aid, SINCE, reason="provider_credit")
    row = writers.get_ticket_analysis(db, aid)
    assert row["budget_wait_reason"] == "provider_credit"

    # Clearing (since=None) always clears the reason too, regardless of arg.
    writers.set_budget_wait_since(db, "ticket_analysis", aid, None, reason="provider_credit")
    row = writers.get_ticket_analysis(db, aid)
    assert row["budget_wait_since"] is None
    assert row["budget_wait_reason"] is None


def test_list_budget_waiting_includes_reason(db):
    inst = _instance(db)
    aid = writers.record_ticket_analysis_created(db, TicketJobParams(
        analysis_id=0, odoo_instance_id=inst, ticket_id=42, model_name="helpdesk.ticket",
        field_name="description", text="t"))
    writers.set_budget_wait_since(db, "ticket_analysis", aid, SINCE, reason="provider_credit")

    rows = writers.list_budget_waiting(db)

    assert rows[0]["budget_wait_reason"] == "provider_credit"


def test_list_budget_waiting_spans_the_four_tables(db):
    inst = _instance(db)
    aid = writers.record_ticket_analysis_created(db, TicketJobParams(
        analysis_id=0, odoo_instance_id=inst, ticket_id=42, model_name="helpdesk.ticket",
        field_name="description", text="t"))
    ts = writers.record_timesheet_run_created(db, TimesheetJobParams(
        run_id=0, odoo_instance_id=inst, request_id="req-7", lines=[]))
    thread = writers.get_or_create_support_thread(
        db, odoo_instance_id=inst, ticket_id=99, model_name="project.task", field_name="f")
    turn = writers.record_support_turn_created(db, thread, inst, "Wie?")
    writers.set_budget_wait_since(db, "ticket_analysis", aid, SINCE)
    writers.set_budget_wait_since(db, "timesheet_review", ts, SINCE)
    writers.set_budget_wait_since(db, "support_answer", turn, SINCE)

    rows = writers.list_budget_waiting(db)

    by_kind = {r["kind"]: r for r in rows}
    assert set(by_kind) == {"ticket_analysis", "timesheet_review", "support_answer"}
    assert by_kind["ticket_analysis"]["record"] == "helpdesk.ticket 42"
    assert by_kind["ticket_analysis"]["instance_name"] == "cu-prod"
    assert by_kind["support_answer"]["record"] == "project.task 99"
    assert by_kind["timesheet_review"]["record"] == "timesheet req-7"
    assert all(r["budget_wait_since"] is not None for r in rows)
    # No reason was passed to set_budget_wait_since here (the plain cap path
    # doesn't), so the column is NULL — the "cap" default lives at the API
    # display layer (api/app/queries/budget_status.py), not in this reader.
    assert by_kind["ticket_analysis"]["budget_wait_reason"] is None


def test_list_budget_waiting_ignores_failed_rows(db):
    """A job that gives up after the max wait leaves budget_wait_since set on its
    now-failed row; list_budget_waiting must filter on status too (Task 3
    review), not just the marker."""
    inst = _instance(db)
    aid = writers.record_ticket_analysis_created(db, TicketJobParams(
        analysis_id=0, odoo_instance_id=inst, ticket_id=42, model_name="helpdesk.ticket",
        field_name="description", text="t"))
    writers.set_budget_wait_since(db, "ticket_analysis", aid, SINCE)
    writers.record_ticket_analysis_failed(db, aid, "gave up")

    assert writers.list_budget_waiting(db) == []


def _review_params(db) -> JobParams:
    repo_id = writers.upsert_repository(db, github_repository_id=1, owner="acme", name="w",
                                        default_branch="main", installation_id=5)
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=9, pr_number=42, title="Add foo",
        author_login="alice", base_branch="main", head_branch="f", head_sha="deadbeef",
        state="open", draft=False)
    return JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
                     installation_id=5, review_mode="diff", trigger_event="opened")


def test_review_waiting_budget_row_and_listing(db):
    params = _review_params(db)
    run_id = writers.record_review_waiting_budget(db, params, SINCE, "cap reached")

    rows = writers.list_reviews_waiting_budget(db)
    assert [r["id"] for r in rows] == [run_id]
    assert rows[0]["repo_full_name"] == "acme/w"
    assert rows[0]["pr_number"] == 42
    assert rows[0]["author_login"] == "alice"
    assert rows[0]["review_mode"] == "diff"
    # Default kind is "cap" (keeps the existing positional call sites working).
    assert rows[0]["budget_wait_reason"] == "cap"
    # A queued Check Run on a waiting row is not a posted review.
    writers.attach_github_ids(db, run_id, check_run_id=777)
    assert writers.is_already_posted(db, params) is False
    # Re-claim works (non-running rows are re-claimable) and clears the marker
    # (and the reason alongside it).
    _, claimed = writers.claim_review_run(db, params, job_id="rq:2")
    assert claimed is True
    assert writers.list_reviews_waiting_budget(db) == []
    with db.session() as s:
        from reva.db.models import ReviewRun
        run = s.get(ReviewRun, run_id)
        assert run.budget_wait_reason is None


def test_get_review_run_budget_wait_reason(db):
    params = _review_params(db)
    writers.record_review_waiting_budget(
        db, params, SINCE, "Waiting for the Anthropic credit balance to be topped up.",
        reason="provider_credit",
    )

    assert writers.get_review_run_budget_wait_reason(db, params) == "provider_credit"


def test_review_waiting_budget_provider_credit_reason(db):
    params = _review_params(db)
    run_id = writers.record_review_waiting_budget(
        db, params, SINCE, "Waiting for the Anthropic credit balance to be topped up.",
        reason="provider_credit",
    )

    rows = writers.list_reviews_waiting_budget(db)
    assert rows[0]["id"] == run_id
    assert rows[0]["budget_wait_reason"] == "provider_credit"


def test_review_spend_kinds_exported():
    assert writers.REVIEW_SPEND_KINDS == ("review", "delta_verify", "triage")
