from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


# --- list_budget_ended_reviews (2026-09-28 auto-requeue) --------------------


NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _repo_pr(db, *, github_repo_id: int, pr_number: int, state: str = "open") -> tuple[int, int]:
    repo_id = writers.upsert_repository(
        db, github_repository_id=github_repo_id, owner="acme", name=f"w{pr_number}",
        default_branch="main", installation_id=5)
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=pr_number, pr_number=pr_number, title="Add foo",
        author_login="alice", base_branch="main", head_branch="f", head_sha="deadbeef",
        state=state, draft=False)
    return repo_id, pr_id


def _age_run(db, run_id: int, completed_at: datetime) -> None:
    from reva.db.models import ReviewRun
    with db.session() as s:
        run = s.get(ReviewRun, run_id)
        run.completed_at = completed_at


def test_list_budget_ended_reviews_credit_failed_open_pr_listed(db):
    repo_id, pr_id = _repo_pr(db, github_repo_id=1, pr_number=42)
    params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
                       installation_id=5, review_mode="diff", trigger_event="opened")
    run_id = writers.record_review_failed(
        db, params, "permanent",
        "Anthropic credit balance too low for the maximum wait; review declined.")
    _age_run(db, run_id, NOW - timedelta(hours=2))

    rows = writers.list_budget_ended_reviews(db, older_than=NOW - timedelta(hours=1))

    assert [r["id"] for r in rows] == [run_id]
    row = rows[0]
    assert row["repository_id"] == repo_id
    assert row["pull_request_id"] == pr_id
    assert row["pr_number"] == 42
    assert row["installation_id"] == 5
    assert row["head_sha"] == "deadbeef"
    assert row["review_mode"] == "diff"
    assert row["status"] == "failed"
    assert row["repo_full_name"] == "acme/w42"


def test_list_budget_ended_reviews_old_credit_text_matches_too(db):
    # The pre-2026-09-27 CLI refusal text, still possible on old rows.
    repo_id, pr_id = _repo_pr(db, github_repo_id=1, pr_number=42)
    params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
                       installation_id=5, review_mode="diff", trigger_event="opened")
    run_id = writers.record_review_failed(db, params, "permanent", "Credit balance is too low.")
    _age_run(db, run_id, NOW - timedelta(hours=2))

    rows = writers.list_budget_ended_reviews(db, older_than=NOW - timedelta(hours=1))
    assert [r["id"] for r in rows] == [run_id]


def test_list_budget_ended_reviews_closed_pr_excluded(db):
    repo_id, pr_id = _repo_pr(db, github_repo_id=1, pr_number=42, state="closed")
    params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
                       installation_id=5, review_mode="diff", trigger_event="opened")
    run_id = writers.record_review_failed(db, params, "permanent", "Credit balance is too low.")
    _age_run(db, run_id, NOW - timedelta(hours=2))

    assert writers.list_budget_ended_reviews(db, older_than=NOW - timedelta(hours=1)) == []


def test_list_budget_ended_reviews_git_error_excluded(db):
    repo_id, pr_id = _repo_pr(db, github_repo_id=1, pr_number=42)
    params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
                       installation_id=5, review_mode="diff", trigger_event="opened")
    run_id = writers.record_review_failed(db, params, "permanent", "git fetch failed: timeout")
    _age_run(db, run_id, NOW - timedelta(hours=2))

    assert writers.list_budget_ended_reviews(db, older_than=NOW - timedelta(hours=1)) == []


def test_list_budget_ended_reviews_cap_declined_listed(db):
    repo_id, pr_id = _repo_pr(db, github_repo_id=1, pr_number=42)
    params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
                       installation_id=5, review_mode="diff", trigger_event="opened")
    run_id = writers.record_review_declined(
        db, params,
        "REVA's rolling 24-hour review budget for @alice ($50) has been reached "
        "(≈15 spent). REVA waited 6 h for it to free up and gave up; REVA retries "
        "by itself once the budget frees up; comment `/review` to retry sooner.")
    _age_run(db, run_id, NOW - timedelta(hours=2))

    rows = writers.list_budget_ended_reviews(db, older_than=NOW - timedelta(hours=1))
    assert [r["id"] for r in rows] == [run_id]
    assert rows[0]["status"] == "declined"


def test_list_budget_ended_reviews_other_decline_excluded(db):
    repo_id, pr_id = _repo_pr(db, github_repo_id=1, pr_number=42)
    params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
                       installation_id=5, review_mode="diff", trigger_event="opened")
    run_id = writers.record_review_declined(db, params, "No reviewable files under custom_addons/")
    _age_run(db, run_id, NOW - timedelta(hours=2))

    assert writers.list_budget_ended_reviews(db, older_than=NOW - timedelta(hours=1)) == []


def test_list_budget_ended_reviews_young_run_excluded(db):
    repo_id, pr_id = _repo_pr(db, github_repo_id=1, pr_number=42)
    params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
                       installation_id=5, review_mode="diff", trigger_event="opened")
    run_id = writers.record_review_failed(db, params, "permanent", "Credit balance is too low.")
    _age_run(db, run_id, NOW - timedelta(minutes=5))

    assert writers.list_budget_ended_reviews(db, older_than=NOW - timedelta(hours=1)) == []


def test_list_budget_ended_reviews_only_latest_run_per_pr(db):
    repo_id, pr_id = _repo_pr(db, github_repo_id=1, pr_number=42)
    old_params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="sha1",
                           installation_id=5, review_mode="diff", trigger_event="opened")
    old_run_id = writers.record_review_failed(db, old_params, "permanent",
                                               "Credit balance is too low.")
    _age_run(db, old_run_id, NOW - timedelta(hours=2))

    # A later push landed on the PR (head_sha now sha2) with a run that isn't
    # a budget failure — the PR's *latest* run is no longer the budget-ended
    # one, so it must not be requeued even though an older budget failure
    # exists underneath it.
    writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=42, pr_number=42, title="Add foo",
        author_login="alice", base_branch="main", head_branch="f", head_sha="sha2",
        state="open", draft=False)
    new_params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="sha2",
                           installation_id=5, review_mode="diff", trigger_event="synchronize")
    writers.record_review_declined(db, new_params, "No reviewable files under custom_addons/")

    assert writers.list_budget_ended_reviews(db, older_than=NOW - timedelta(hours=1)) == []


def test_list_budget_ended_reviews_newer_pending_row_excluded(db):
    """A fresh push (or this loop's own prior requeue) already queued a
    pending_reviews row scheduled after the run ended — must not be clobbered
    with the stale SHA or duplicated (Critical 1 + 2, review 2026-09-28)."""
    repo_id, pr_id = _repo_pr(db, github_repo_id=1, pr_number=42)
    params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
                       installation_id=5, review_mode="diff", trigger_event="opened")
    run_id = writers.record_review_failed(db, params, "permanent", "Credit balance is too low.")
    _age_run(db, run_id, NOW - timedelta(hours=2))
    writers.upsert_pending_review(
        db, repository_id=repo_id, pull_request_id=pr_id, pr_number=42, head_sha="deadbeef",
        installation_id=5, trigger_event="manual_requeue", review_mode="diff",
        scheduled_at=NOW - timedelta(minutes=90))

    assert writers.list_budget_ended_reviews(db, older_than=NOW - timedelta(hours=1)) == []


def test_list_budget_ended_reviews_head_moved_excluded(db):
    """The run's head_sha is no longer the PR's current head — a later push
    needs its own review, not a requeue of a stale SHA (Critical 1)."""
    repo_id, pr_id = _repo_pr(db, github_repo_id=1, pr_number=42)
    params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
                       installation_id=5, review_mode="diff", trigger_event="opened")
    run_id = writers.record_review_failed(db, params, "permanent", "Credit balance is too low.")
    _age_run(db, run_id, NOW - timedelta(hours=2))
    writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=42, pr_number=42, title="Add foo",
        author_login="alice", base_branch="main", head_branch="f", head_sha="sha2",
        state="open", draft=False)

    assert writers.list_budget_ended_reviews(db, older_than=NOW - timedelta(hours=1)) == []


def test_list_budget_ended_reviews_draft_pr_excluded(db):
    repo_id, pr_id = _repo_pr(db, github_repo_id=1, pr_number=42)
    writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=42, pr_number=42, title="Add foo",
        author_login="alice", base_branch="main", head_branch="f", head_sha="deadbeef",
        state="open", draft=True)
    params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
                       installation_id=5, review_mode="diff", trigger_event="opened")
    run_id = writers.record_review_failed(db, params, "permanent", "Credit balance is too low.")
    _age_run(db, run_id, NOW - timedelta(hours=2))

    assert writers.list_budget_ended_reviews(db, older_than=NOW - timedelta(hours=1)) == []


def test_list_budget_ended_reviews_schema_validation_false_positive_excluded(db):
    """'credit balance' is an ordinary accounting phrase that can show up in a
    Claude finding's own text on a schema-validation failure — must not match
    REVA's own budget-refusal texts (Important 3, review 2026-09-28)."""
    repo_id, pr_id = _repo_pr(db, github_repo_id=1, pr_number=42)
    params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
                       installation_id=5, review_mode="diff", trigger_event="opened")
    run_id = writers.record_review_failed(
        db, params, "permanent",
        "Claude finding failed schema validation: credit balance moves must...")
    _age_run(db, run_id, NOW - timedelta(hours=2))

    assert writers.list_budget_ended_reviews(db, older_than=NOW - timedelta(hours=1)) == []
