"""Tests for OdooCallbackClient.write_field.

Patches reva.odoo_client.httpx.post via monkeypatch — no live network calls.
"""

from __future__ import annotations

import json
from typing import Literal

import httpx
import pytest
from pydantic import BaseModel

from reva.errors import PermanentError, TransientError
from reva.odoo_client import OdooCallbackClient

_URL = "https://odoo.example.com/api/reva/write-field"
_KEY = "test-api-key-secret"


def _client() -> OdooCallbackClient:
    return OdooCallbackClient(callback_url=_URL, api_key=_KEY)


def _kwargs() -> dict:
    return {
        "ticket_id": 123,
        "model_name": "helpdesk.ticket",
        "field_name": "description",
        "html": "<h2>Summary</h2><p>test</p>",
    }


def _mock_post(status: int = 200, body: str = '{"ok":true}', exc=None):
    """Return a callable that replaces httpx.post."""
    def post(url, **kwargs):
        if exc is not None:
            raise exc
        return httpx.Response(status, text=body)
    return post


def test_empty_callback_url_constructs_disabled():
    # Empty URL = Odoo callback disabled (settings contract). Constructing must
    # NOT validate or raise — the worker boots without Odoo configured.
    OdooCallbackClient(callback_url="", api_key="")


def test_disabled_client_write_field_raises_permanent():
    client = OdooCallbackClient(callback_url="", api_key="")
    with pytest.raises(PermanentError):
        client.write_field(**_kwargs())


def test_disabled_client_reset_status_raises_permanent():
    client = OdooCallbackClient(callback_url="", api_key="")
    with pytest.raises(PermanentError):
        client.reset_status(ticket_id=123, model_name="helpdesk.ticket", analysis_id=7)


def test_rejects_cloud_metadata_callback_url():
    with pytest.raises(ValueError):
        OdooCallbackClient(callback_url="http://169.254.169.254/write-field", api_key=_KEY)


def test_allows_internal_callback_url():
    # Odoo is commonly on an internal network — must be permitted.
    OdooCallbackClient(callback_url="http://10.0.0.9:8069/api/reva/write-field", api_key=_KEY)


def test_write_field_success(monkeypatch):
    monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(200))
    _client().write_field(**_kwargs())  # no exception


def test_write_field_4xx_is_permanent(monkeypatch):
    for status in (400, 401, 403, 404, 422):
        monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(status, "bad request"))
        with pytest.raises(PermanentError):
            _client().write_field(**_kwargs())


def test_write_field_5xx_is_transient(monkeypatch):
    monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(500, "server error"))
    with pytest.raises(TransientError):
        _client().write_field(**_kwargs())


def test_write_field_timeout_is_transient(monkeypatch):
    exc = httpx.TimeoutException("timed out")
    monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(exc=exc))
    with pytest.raises(TransientError):
        _client().write_field(**_kwargs())


def test_write_field_transport_error_is_transient(monkeypatch):
    exc = httpx.TransportError("connection refused")
    monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(exc=exc))
    with pytest.raises(TransientError):
        _client().write_field(**_kwargs())


def test_write_field_sends_bearer_token(monkeypatch):
    captured: dict = {}

    def post(url, *, headers, **kwargs):
        captured["auth"] = headers.get("Authorization", "")
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    _client().write_field(**_kwargs())
    assert captured["auth"] == f"Bearer {_KEY}"


def test_write_field_payload_shape(monkeypatch):
    captured: dict = {}

    def post(url, *, json, **kwargs):
        captured["body"] = json
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    kw = _kwargs()
    _client().write_field(**kw)
    assert captured["body"]["ticket_id"] == kw["ticket_id"]
    assert captured["body"]["model_name"] == kw["model_name"]
    assert captured["body"]["field_name"] == kw["field_name"]
    assert captured["body"]["html"] == kw["html"]


# --- /tickets/ namespacing (Odoo-side API change, 2026-07-05) -------------------
# callback_url is the Odoo app's REVA API base; ticket endpoints live under
# /tickets/. Legacy configs that stored the old write-field endpoint keep
# working — the suffix is stripped at construction.


def _capture_url(monkeypatch) -> dict:
    captured: dict = {}

    def post(url, **kwargs):
        captured["url"] = url
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    return captured


@pytest.mark.parametrize("configured", [
    "https://odoo.example.com/api/reva",                        # new: plain base
    "https://odoo.example.com/api/reva/",                       # base, trailing slash
    "https://odoo.example.com/api/reva/write-field",            # legacy stored form
    "https://odoo.example.com/api/reva/tickets/write-field",    # fully-qualified new form
])
def test_write_field_url_from_any_configured_form(monkeypatch, configured):
    captured = _capture_url(monkeypatch)
    OdooCallbackClient(callback_url=configured, api_key=_KEY).write_field(**_kwargs())
    assert captured["url"] == "https://odoo.example.com/api/reva/tickets/write-field"


def test_reset_status_uses_tickets_namespace(monkeypatch):
    captured = _capture_url(monkeypatch)
    _client().reset_status(ticket_id=123, model_name="helpdesk.ticket", analysis_id=7)
    assert captured["url"] == "https://odoo.example.com/api/reva/tickets/reset-status"


# --- timesheet_results -------------------------------------------------------


def _ts_kwargs() -> dict:
    return {
        "request_id": "req-1",
        "results": [
            {"line_id": 2, "status": "rewritten", "updated_desc": "Implemented reports"}
        ],
        "stats": {"total": 3, "ok": 2, "rewritten": 1, "needs_human": 0},
    }


def test_timesheet_results_posts_contract(monkeypatch):
    captured = {}

    def post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs["json"]
        captured["headers"] = kwargs["headers"]
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    _client().timesheet_results(**_ts_kwargs())
    assert captured["url"] == "https://odoo.example.com/api/reva/hr/timesheet-results"
    assert captured["json"] == _ts_kwargs()
    assert captured["headers"]["Authorization"] == f"Bearer {_KEY}"


def test_timesheet_results_4xx_permanent(monkeypatch):
    monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(status=409))
    with pytest.raises(PermanentError):
        _client().timesheet_results(**_ts_kwargs())


def test_timesheet_results_5xx_transient(monkeypatch):
    monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(status=502))
    with pytest.raises(TransientError):
        _client().timesheet_results(**_ts_kwargs())


def test_timesheet_results_disabled_client_permanent():
    with pytest.raises(PermanentError):
        OdooCallbackClient(callback_url="", api_key="").timesheet_results(**_ts_kwargs())


# --- issues_created (github-issues handoff, Contract 2) ------------------------


def _issues_kwargs() -> dict:
    return {
        "ticket_id": 123,
        "model_name": "helpdesk.ticket",
        "request_id": 7,
        "status": "created",
        "issues": [
            {"number": 42, "title": "Implement login form",
             "url": "https://github.com/org/repo/issues/42"},
        ],
        "error": None,
    }


def test_issues_created_posts_contract_payload_to_sibling_path(monkeypatch):
    captured: dict = {}

    def post(url, *, json, headers, **kwargs):
        captured["url"] = url
        captured["body"] = json
        captured["auth"] = headers.get("Authorization", "")
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    _client().issues_created(**_issues_kwargs())

    # base URL is derived from the configured callback URL — no new config
    assert captured["url"] == "https://odoo.example.com/api/reva/tickets/issues-created"
    assert captured["auth"] == f"Bearer {_KEY}"
    expected = {**_issues_kwargs(), "total_estimate_hours": None}
    assert captured["body"] == expected


def test_issues_created_failed_status(monkeypatch):
    captured: dict = {}

    def post(url, *, json, **kwargs):
        captured["body"] = json
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    _client().issues_created(
        ticket_id=1, model_name="project.task", request_id=9,
        status="failed", issues=[], error="GitHub authentication failed",
    )
    assert captured["body"]["status"] == "failed"
    assert captured["body"]["issues"] == []
    assert captured["body"]["error"] == "GitHub authentication failed"


def test_issues_created_409_is_permanent(monkeypatch):
    # 409 = record no longer pending / stale request_id — contract says do not retry
    monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(409, "conflict"))
    with pytest.raises(PermanentError):
        _client().issues_created(**_issues_kwargs())


def test_issues_created_5xx_is_transient(monkeypatch):
    monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(503, "down"))
    with pytest.raises(TransientError):
        _client().issues_created(**_issues_kwargs())


def test_disabled_client_issues_created_raises_permanent():
    client = OdooCallbackClient(callback_url="", api_key="")
    with pytest.raises(PermanentError):
        client.issues_created(**_issues_kwargs())


def test_issues_created_includes_estimates_and_total(monkeypatch):
    captured: dict = {}

    def post(url, *, json, **kwargs):
        captured["body"] = json
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    kwargs = _issues_kwargs()
    kwargs["issues"] = [
        {"number": 42, "title": "Implement login form",
         "url": "https://github.com/org/repo/issues/42", "estimate_hours": 2.5},
        {"number": 43, "title": "No estimate",
         "url": "https://github.com/org/repo/issues/43"},
    ]
    _client().issues_created(**kwargs, total_estimate_hours=2.5)
    items = captured["body"]["issues"]
    assert items[0]["estimate_hours"] == 2.5
    # Pre-rollout items simply omit the key (optional-key omission).
    assert "estimate_hours" not in items[1]
    assert captured["body"]["total_estimate_hours"] == 2.5


def test_issue_state_and_ready_snapshots_carry_estimate(monkeypatch):
    captured: list[dict] = []

    def post(url, *, json, **kwargs):
        captured.append(json)
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    snapshot = [{"number": 42, "title": "t", "url": "https://gh/42",
                 "state": "closed", "estimate_hours": 3.0}]
    _client().issue_state(ticket_id=1, model_name="helpdesk.ticket",
                          number=42, state="closed", issues=snapshot)
    _client().tickets_ready(ticket_id=1, model_name="helpdesk.ticket",
                            issues=snapshot)
    assert all(body["issues"][0]["estimate_hours"] == 3.0 for body in captured)


# --- issue_state (per-issue done/reopen sync) -----------------------------------


def test_issue_state_posts_snapshot_to_sibling_path(monkeypatch):
    captured: dict = {}

    def post(url, *, json, headers, **kwargs):
        captured["url"] = url
        captured["body"] = json
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    snapshot = [
        {"number": 42, "title": "Implement login form",
         "url": "https://github.com/org/repo/issues/42", "state": "closed"},
        {"number": 43, "title": "Add session handling",
         "url": "https://github.com/org/repo/issues/43", "state": "open"},
    ]
    _client().issue_state(
        ticket_id=123, model_name="helpdesk.ticket",
        number=42, state="closed", issues=snapshot,
    )

    assert captured["url"] == "https://odoo.example.com/api/reva/tickets/issue-state"
    assert captured["body"] == {
        "ticket_id": 123, "model_name": "helpdesk.ticket",
        "number": 42, "state": "closed", "issues": snapshot,
    }


def test_issue_refs_carry_dates_and_strip_internal_keys(monkeypatch):
    """Every ref sent to Odoo carries exactly the seven documented keys; the
    internal node_id/project_item_id never leak (spec decision 6)."""
    captured: dict = {}

    def post(url, *, json, **kwargs):
        captured["body"] = json
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    snapshot = [{
        "number": 42, "title": "Implement login form",
        "url": "https://github.com/org/repo/issues/42", "state": "closed",
        "plan_date": "2026-07-15", "complete_date": "2026-07-09",
        "node_id": "I_1", "project_item_id": "PVTI_1", "id": 900, "attached": True,
    }]
    _client().issues_created(
        ticket_id=1, model_name="project.task", request_id=9,
        status="created", issues=snapshot, error=None,
    )
    assert captured["body"]["issues"] == [{
        "number": 42, "title": "Implement login form",
        "url": "https://github.com/org/repo/issues/42", "state": "closed",
        "plan_date": "2026-07-15", "complete_date": "2026-07-09",
    }]

    _client().issue_state(
        ticket_id=1, model_name="project.task", number=42, state="closed",
        issues=snapshot,
    )
    assert set(captured["body"]["issues"][0]) == {
        "number", "title", "url", "state", "plan_date", "complete_date"}


def test_issue_state_409_is_permanent(monkeypatch):
    monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(409, "conflict"))
    with pytest.raises(PermanentError):
        _client().issue_state(ticket_id=1, model_name="project.task",
                              number=1, state="closed", issues=[])


# --- issue_work_status / change_summary (spec 2026-07-11) ----------------------


def test_issue_work_status_posts_contract(monkeypatch):
    captured: dict = {}

    def post(url, *, json, headers, **kwargs):
        captured["url"] = url
        captured["body"] = json
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    _client().issue_work_status(
        ticket_id=123, model_name="helpdesk.ticket",
        issues=[{"number": 42, "work_status": "in_progress"}],
    )
    assert captured["url"] == "https://odoo.example.com/api/reva/tickets/issue-work-status"
    assert captured["body"] == {
        "ticket_id": 123, "model_name": "helpdesk.ticket",
        "issues": [{"number": 42, "work_status": "in_progress"}],
    }


def test_issue_work_status_disabled_client_permanent():
    with pytest.raises(PermanentError):
        OdooCallbackClient(callback_url="", api_key="").issue_work_status(
            ticket_id=1, model_name="project.task",
            issues=[{"number": 1, "work_status": "in_review"}],
        )


def test_change_summary_posts_contract(monkeypatch):
    captured: dict = {}

    def post(url, *, json, headers, **kwargs):
        captured["url"] = url
        captured["body"] = json
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    notes = [{
        "pr": {"number": 7, "title": "Login rework",
               "url": "https://github.com/acme/widgets/pull/7", "repo": "acme/widgets"},
        "note_html": "<p>merged</p>",
    }]
    _client().change_summary(ticket_id=123, model_name="helpdesk.ticket", notes=notes)
    assert captured["url"] == "https://odoo.example.com/api/reva/tickets/change-summary"
    assert captured["body"] == {
        "ticket_id": 123, "model_name": "helpdesk.ticket", "notes": notes,
    }


def test_change_summary_5xx_is_transient(monkeypatch):
    monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(503, "down"))
    with pytest.raises(TransientError):
        _client().change_summary(ticket_id=1, model_name="project.task", notes=[])


def test_change_summary_disabled_client_permanent():
    with pytest.raises(PermanentError):
        OdooCallbackClient(callback_url="", api_key="").change_summary(
            ticket_id=1, model_name="project.task", notes=[],
        )


def test_change_summary_carries_the_release_log_block_only_when_given(monkeypatch):
    seen: list[dict] = []

    def post(url, **kwargs):
        seen.append(kwargs["json"])
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    notes = [{"pr": {"number": 7, "title": "t", "url": "https://github.com/acme/widgets/pull/7",
                     "repo": "acme/widgets"}, "note_html": ""}]
    client = _client()
    client.change_summary(ticket_id=97, model_name="helpdesk.ticket", notes=notes)
    client.change_summary(
        ticket_id=97, model_name="helpdesk.ticket", notes=notes,
        release_log={"release": "lollipop", "ticket": 97, "title": "Login", "status": "umgesetzt",
                     "modules": ["cu_auth 19.0.1.0.0"], "html": "<p><strong>Gebaut</strong></p><p>x</p>"},
    )
    assert "release_log" not in seen[0]
    assert seen[0]["notes"][0]["note_html"] == ""
    assert seen[1]["release_log"]["title"] == "Login"
    assert seen[1]["release_log"]["modules"] == ["cu_auth 19.0.1.0.0"]


# --- release_note (release-log lookup, spec 2026-09-04) ------------------------


def test_release_note_posts_completed_payload(monkeypatch):
    seen: dict = {}

    def post(url, **kwargs):
        seen["url"] = url
        seen["json"] = kwargs["json"]
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)

    _client().release_note(
        release_id=3275, note_id=7, status="completed",
        url="https://reva.example.com/docs/?repo=1&path=docs/releases/lollipop.html",
        html='<div class="rl-page"></div>', css=".rl-page{}",
    )

    assert seen["url"] == "https://odoo.example.com/api/reva/releases/release-note"
    assert seen["json"] == {
        "release_id": 3275, "note_id": 7, "status": "completed",
        "url": "https://reva.example.com/docs/?repo=1&path=docs/releases/lollipop.html",
        "html": '<div class="rl-page"></div>', "css": ".rl-page{}",
    }
    assert "error" not in seen["json"]


def test_release_note_failed_payload_and_409_is_permanent(monkeypatch):
    seen: dict = {}

    def post(url, **kwargs):
        seen["json"] = kwargs["json"]
        return httpx.Response(409, text='{"detail":"Stale note_id"}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)

    with pytest.raises(PermanentError):
        _client().release_note(
            release_id=3275, note_id=7, status="failed",
            error="Kein Release-Log 'docs/releases/lollipop.html' in acme/widgets",
        )
    assert seen["json"] == {
        "release_id": 3275, "note_id": 7, "status": "failed",
        "error": "Kein Release-Log 'docs/releases/lollipop.html' in acme/widgets",
    }


class _OdooReleaseNoteRequest(BaseModel):
    """Verbatim copy of the shipped Odoo callback model
    (Cloudunify/custom_addons/cu_reva_ticket_analysis/routers/release_router.py):
    `html` is a plain str, so a null would 422 there. Guards the wire shape."""

    release_id: int
    html: str = ""
    note_id: int = 0
    status: Literal["completed", "failed"] = "completed"
    error: str | None = None


def test_release_note_payloads_validate_against_shipped_odoo_model(monkeypatch):
    seen: list[dict] = []

    def post(url, **kwargs):
        seen.append(kwargs["json"])
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    client = _client()
    client.release_note(release_id=1, note_id=2, status="completed", url="https://reva.example.com/docs/?repo=1&path=docs/releases/x.html", html="<div class=\"rl-page\"></div>", css=".rl-page{}")
    client.release_note(release_id=1, note_id=3, status="failed", error="Kein Release-Log 'docs/releases/x.html' in acme/widgets")

    assert len(seen) == 2
    for body in seen:
        _OdooReleaseNoteRequest.model_validate(body)  # must not raise


# --- Correlation ids on write-field --------------------------------------------
# Odoo's staleness guard rejects a callback whose id no longer matches the one
# the record stored at submit time. Both legs carry their own id: the support
# leg `turn_id`, the analysis leg `analysis_id`.


def test_write_field_carries_turn_id(monkeypatch):
    captured: dict = {}

    def post(url, *, json, **kwargs):
        captured["body"] = json
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    _client().write_field(**_kwargs(), turn_id=987)
    assert captured["body"]["turn_id"] == 987


def test_write_field_carries_analysis_id(monkeypatch):
    captured: dict = {}

    def post(url, *, json, **kwargs):
        captured["body"] = json
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    _client().write_field(**_kwargs(), analysis_id=456)
    assert captured["body"]["analysis_id"] == 456


def test_write_field_correlation_ids_default_to_zero(monkeypatch):
    # 0 = "not sent" on the Odoo side: its guard is `if body.analysis_id and
    # ... != stored`, so a zero never rejects. Callers that have no id omit it.
    captured: dict = {}

    def post(url, *, json, **kwargs):
        captured["body"] = json
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    _client().write_field(**_kwargs())
    assert captured["body"]["turn_id"] == 0
    assert captured["body"]["analysis_id"] == 0
