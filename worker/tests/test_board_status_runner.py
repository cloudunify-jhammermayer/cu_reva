"""Board-status job tests — fakes only, no network."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from reva.db.engine import Database, create_engine_from_url
from reva.db.models import Base, OpsEvent, TicketIssueRun
from reva.errors import PermanentError, TransientError
from worker.board_status_runner import run_board_status_update
from worker.runner import WorkerContext, set_context

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


def test_no_refs_anywhere_is_noop_without_board_calls(db):
    _seed_board_issue(db)
    ctx = _ctx(db, pr_body="plain refactor", closing_numbers=[])
    out = run_board_status_update(_params())
    assert out == {"status": "no_board_items"}
    ctx.github.get_project.assert_not_called()
    ctx.github.get_file_content.assert_not_called()  # config only fetched when items exist


def test_kill_switch_disables(db):
    _seed_board_issue(db)
    ctx = _ctx(db, config_yaml="board_status_sync: false\n")
    out = run_board_status_update(_params())
    assert out == {"status": "disabled"}
    ctx.github.set_project_item_option.assert_not_called()


def test_config_parse_error_fails_open(db):
    _seed_board_issue(db)
    ctx = _ctx(db, config_yaml=":: not yaml ::[")
    out = run_board_status_update(_params())
    assert out["moved"] == 1


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
    assert out == {"status": "no_board_items"}
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
