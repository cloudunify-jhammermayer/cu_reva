"""Board-status job tests — fakes only, no network."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from reva.db.engine import Database, create_engine_from_url
from reva.db.models import Base, OpsEvent, TicketIssueRun
from reva.errors import PermanentError, TransientError
from worker.board_status_runner import run_board_status_update
from worker.runner import WorkerContext, set_context


@dataclass
class FakeOdoo:
    """Records issue_work_status callbacks (the Odoo leg). Board tests reach it
    via the autouse build_odoo_client patch; instance rows are never seeded."""

    raise_exc: Exception | None = None
    calls: list[dict] = field(default_factory=list)

    def issue_work_status(self, ticket_id, model_name, issues):
        self.calls.append(
            {"ticket_id": ticket_id, "model_name": model_name, "issues": issues}
        )
        if self.raise_exc:
            raise self.raise_exc


@pytest.fixture(autouse=True)
def odoo(monkeypatch):
    fake = FakeOdoo()
    monkeypatch.setattr(
        "worker.board_status_runner.build_odoo_client", lambda ctx, _id: fake
    )
    return fake

_URL = "https://github.com/orgs/acme/projects/7"
_PROJECT = {
    "id": "PVT_1",
    "fields": [
        {"id": "F_STATUS", "name": "Status", "dataType": "SINGLE_SELECT",
         "options": [
             {"id": "OPT_TODO", "name": "Todo"},
             {"id": "OPT_PROG", "name": "In Progress"},
             {"id": "OPT_REV", "name": "In review"},
             {"id": "OPT_DONE", "name": "Done"},
         ]},
    ],
}


@pytest.fixture()
def db():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _seed_board_issue(db, *, number=50, state="open", item_id="PVTI_50",
                      url=_URL, ticket_id=97):
    with db.session() as s:
        s.add(TicketIssueRun(
            odoo_instance_id=1, ticket_id=ticket_id, model_name="helpdesk.ticket",
            github_url="https://github.com/acme/widgets",
            repo_full_name="acme/widgets", status="completed",
            name="t", description="d", analysis_html="<p/>",
            priority="1", ticket_url=f"https://odoo.example/tickets/{ticket_id}",
            github_project_url=url,
            issues=[{"number": number, "title": "t", "url": f"https://gh/{number}",
                     "state": state, "project_item_id": item_id}],
        ))


def _ctx(db, *, pr_body="Closes #50", config_yaml=None, closing_numbers=None,
         project=_PROJECT):
    github = MagicMock()
    github.get_installation_token.return_value = "tok"
    github.get_pull_request.return_value = {"body": pr_body, "head": {"sha": "abc"}}
    github.get_file_content.return_value = config_yaml
    github.get_closing_issue_numbers.return_value = closing_numbers or []
    github.get_project.return_value = project
    ctx = WorkerContext(
        db=db, claude=MagicMock(), runner=None, github=github,  # type: ignore[arg-type]
        reviewer=None, auditor=None, ticket_analyzer=None, verifier=None,  # type: ignore[arg-type]
    )
    set_context(ctx)
    return ctx


def _params(trigger="pr_active"):
    return {"repo_full_name": "acme/widgets", "pr_number": 42,
            "installation_id": 99, "trigger": trigger}


def _ops_events(db, *, limit=10):
    """Test-only ops-events reader.

    reva.db.writers has no `list_ops_events` — the reader for that table lives
    in api/app/queries/ops_events.py, a different service's package the worker
    tests shouldn't import. Query the OpsEvent model directly instead; the
    assertion intent (an event with a given name exists / doesn't) is what
    matters, not the exact reader used.
    """
    with db.session() as s:
        rows = s.execute(
            select(OpsEvent).order_by(OpsEvent.id.desc()).limit(limit)
        ).scalars().all()
    return [{"event": r.event, "component": r.component, "detail": r.detail} for r in rows]


def test_pr_active_sets_in_progress(db):
    _seed_board_issue(db)
    ctx = _ctx(db)
    out = run_board_status_update(_params("pr_active"))
    assert out == {"status": "completed", "moved": 1}
    ctx.github.set_project_item_option.assert_called_once_with(
        "tok", "PVT_1", "PVTI_50", "F_STATUS", "OPT_PROG")


def test_review_done_sets_in_review(db):
    _seed_board_issue(db)
    ctx = _ctx(db)
    run_board_status_update(_params("review_done"))
    ctx.github.set_project_item_option.assert_called_once_with(
        "tok", "PVT_1", "PVTI_50", "F_STATUS", "OPT_REV")


def test_sidebar_only_link_found_via_graphql_fallback(db):
    _seed_board_issue(db)
    ctx = _ctx(db, pr_body="no closing keywords", closing_numbers=[50])
    out = run_board_status_update(_params())
    assert out["moved"] == 1
    ctx.github.get_closing_issue_numbers.assert_called_once()


def test_no_refs_anywhere_is_noop_without_board_calls(db, odoo):
    _seed_board_issue(db)
    ctx = _ctx(db, pr_body="plain refactor", closing_numbers=[])
    out = run_board_status_update(_params())
    # No linked issues → neither leg does anything, and config is never fetched.
    assert out == {"status": "no_refs"}
    ctx.github.get_project.assert_not_called()
    ctx.github.get_file_content.assert_not_called()
    assert odoo.calls == []


def test_board_kill_switch_disables_board_not_work_status(db, odoo):
    # The two switches are independent: board_status_sync: false stops the board
    # leg but the Odoo work-status leg still fires (work_status defaults on).
    _seed_board_issue(db)
    ctx = _ctx(db, config_yaml="board_status_sync: false\n")
    out = run_board_status_update(_params())
    assert out == {"status": "disabled"}
    ctx.github.set_project_item_option.assert_not_called()
    assert [c["ticket_id"] for c in odoo.calls] == [97]
    assert odoo.calls[0]["issues"] == [{"number": 50, "work_status": "in_progress"}]


def test_config_fetch_failure_fails_open_with_ops_event(db):
    _seed_board_issue(db)
    ctx = _ctx(db)
    ctx.github.get_file_content.side_effect = RuntimeError("config unreadable")
    out = run_board_status_update(_params())
    # fail-open: a config hiccup must not freeze the board...
    assert out["moved"] == 1
    # ...but it is a visible degradation (CLAUDE.md invariant), not silent.
    events = _ops_events(db, limit=10)
    assert any(e["event"] == "config_fetch_failed" and e["component"] == "board_status"
               for e in events)


def test_missing_option_is_silent_skip_no_ops_event(db):
    project = {"id": "PVT_1", "fields": [
        {"id": "F_STATUS", "name": "Status", "dataType": "SINGLE_SELECT",
         "options": [{"id": "OPT_TODO", "name": "Todo"}]}]}
    _seed_board_issue(db)
    ctx = _ctx(db, project=project)
    out = run_board_status_update(_params())
    assert out == {"status": "completed", "moved": 0}
    ctx.github.set_project_item_option.assert_not_called()
    assert _ops_events(db, limit=10) == []  # config, not degradation


def test_transient_set_failure_reraises_for_rq_retry(db):
    _seed_board_issue(db)
    ctx = _ctx(db)
    ctx.github.set_project_item_option.side_effect = TransientError("503")
    with pytest.raises(TransientError):
        run_board_status_update(_params())


def test_permanent_set_failure_swallowed_with_ops_event(db):
    _seed_board_issue(db)
    ctx = _ctx(db)
    ctx.github.set_project_item_option.side_effect = PermanentError("422")
    out = run_board_status_update(_params())
    assert out == {"status": "completed", "moved": 0}
    events = _ops_events(db, limit=10)
    assert any(e["event"] == "set_option_failed" for e in events)


def test_graphql_link_lookup_failure_degrades_with_ops_event(db):
    _seed_board_issue(db)
    ctx = _ctx(db, pr_body="no refs")
    ctx.github.get_closing_issue_numbers.side_effect = PermanentError("boom")
    out = run_board_status_update(_params())
    assert out == {"status": "no_refs"}
    events = _ops_events(db, limit=10)
    assert any(e["event"] == "link_resolution_failed" for e in events)


def test_one_get_project_per_board_for_multiple_items(db):
    _seed_board_issue(db, number=50, item_id="PVTI_50", ticket_id=97)
    _seed_board_issue(db, number=51, item_id="PVTI_51", ticket_id=98)
    ctx = _ctx(db, pr_body="Closes #50 fixes #51")
    out = run_board_status_update(_params())
    assert out["moved"] == 2
    assert ctx.github.get_project.call_count == 1


def test_pr_fetch_permanent_failure_swallowed_with_ops_event(db):
    _seed_board_issue(db)
    ctx = _ctx(db)
    ctx.github.get_pull_request.side_effect = PermanentError("404")
    out = run_board_status_update(_params())
    assert out == {"status": "failed"}
    events = _ops_events(db, limit=10)
    assert any(e["event"] == "pr_fetch_failed" for e in events)
    ctx.github.get_project.assert_not_called()
    ctx.github.set_project_item_option.assert_not_called()


def test_pr_fetch_transient_failure_reraises_for_rq_retry(db):
    _seed_board_issue(db)
    ctx = _ctx(db)
    ctx.github.get_pull_request.side_effect = TransientError("503")
    with pytest.raises(TransientError):
        run_board_status_update(_params())


def test_review_done_on_merged_pr_is_pr_closed_noop(db):
    _seed_board_issue(db)
    ctx = _ctx(db)
    ctx.github.get_pull_request.return_value = {
        "body": "Closes #50", "state": "closed", "merged": True,
        "head": {"sha": "abc"},
    }
    out = run_board_status_update(_params("review_done"))
    assert out == {"status": "pr_closed"}
    ctx.github.get_project.assert_not_called()
    ctx.github.set_project_item_option.assert_not_called()


# --- Odoo work-status leg (spec 2026-07-11) -----------------------------------


def test_pr_active_sends_in_progress_work_status(db, odoo):
    _seed_board_issue(db)
    _ctx(db)
    run_board_status_update(_params("pr_active"))
    assert odoo.calls == [{
        "ticket_id": 97, "model_name": "helpdesk.ticket",
        "issues": [{"number": 50, "work_status": "in_progress"}],
    }]


def test_review_done_sends_in_review_work_status(db, odoo):
    _seed_board_issue(db)
    _ctx(db)
    run_board_status_update(_params("review_done"))
    assert odoo.calls[0]["issues"] == [{"number": 50, "work_status": "in_review"}]


def test_board_less_ticket_still_gets_work_status(db, odoo):
    # No board URL on the run → no board items, but the Odoo leg still fires.
    _seed_board_issue(db, url=None)
    ctx = _ctx(db)
    out = run_board_status_update(_params("pr_active"))
    assert out == {"status": "no_board_items"}
    assert odoo.calls[0]["issues"] == [{"number": 50, "work_status": "in_progress"}]
    ctx.github.set_project_item_option.assert_not_called()


def test_work_status_kill_switch_off_no_callback_board_unaffected(db, odoo):
    _seed_board_issue(db)
    ctx = _ctx(db, config_yaml="work_status: false\n")
    out = run_board_status_update(_params("pr_active"))
    # Board leg unaffected (moves the card); Odoo leg silenced.
    assert out == {"status": "completed", "moved": 1}
    ctx.github.set_project_item_option.assert_called_once()
    assert odoo.calls == []


def test_work_status_permanent_error_records_ops_event_and_continues(db, odoo):
    _seed_board_issue(db)
    ctx = _ctx(db)
    odoo.raise_exc = PermanentError("Odoo /issue-work-status 409")
    out = run_board_status_update(_params("pr_active"))
    # Board leg still moves the card; the Odoo rejection is visible, not fatal.
    assert out == {"status": "completed", "moved": 1}
    ctx.github.set_project_item_option.assert_called_once()
    events = _ops_events(db, limit=10)
    assert any(e["event"] == "work_status_rejected" and e["component"] == "odoo_callback"
               for e in events)


def test_work_status_transient_error_reraises_for_rq_retry(db, odoo):
    _seed_board_issue(db)
    _ctx(db)
    odoo.raise_exc = TransientError("Odoo 503")
    with pytest.raises(TransientError):
        run_board_status_update(_params("pr_active"))


def test_pr_active_on_merged_pr_is_pr_closed_noop(db, odoo):
    _seed_board_issue(db)
    ctx = _ctx(db)
    ctx.github.get_pull_request.return_value = {
        "body": "Closes #50", "state": "closed", "merged": True,
        "head": {"sha": "abc"},
    }
    out = run_board_status_update(_params("pr_active"))
    assert out == {"status": "pr_closed"}
    assert odoo.calls == []
    ctx.github.get_project.assert_not_called()


def test_single_config_fetch_serves_both_flags(db, odoo):
    _seed_board_issue(db)
    ctx = _ctx(db)
    run_board_status_update(_params("pr_active"))
    # Both the work-status leg and the board leg run, but the repo config is
    # fetched exactly once.
    assert ctx.github.get_file_content.call_count == 1
    assert odoo.calls  # work leg ran
    ctx.github.set_project_item_option.assert_called_once()  # board leg ran
