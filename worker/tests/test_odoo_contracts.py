"""Contract payload models: wire shapes and validating samples."""

from __future__ import annotations

from reva.odoo_contracts import (
    CONTRACTS,
    IssueStatePayload,
    IssuesCreatedPayload,
    ResetStatusPayload,
    TimesheetResultsPayload,
    WriteFieldPayload,
)


def test_write_field_wire_shape():
    payload = WriteFieldPayload(
        ticket_id=123,
        model_name="helpdesk.ticket",
        field_name="description",
        html="<h2>x</h2>",
    )
    assert payload.model_dump() == {
        "ticket_id": 123,
        "model_name": "helpdesk.ticket",
        "field_name": "description",
        "html": "<h2>x</h2>",
    }


def test_reset_status_wire_shape():
    # analysis_id is required on the wire: Odoo's staleness guard needs the
    # correlation id to reject stale/replayed resets (409 there).
    assert ResetStatusPayload(
        ticket_id=1, model_name="project.task", analysis_id=42
    ).model_dump() == {
        "ticket_id": 1,
        "model_name": "project.task",
        "analysis_id": 42,
    }


def test_issues_created_wire_shape_and_extra_keys_stripped():
    payload = IssuesCreatedPayload(
        ticket_id=1,
        model_name="helpdesk.ticket",
        request_id=7,
        status="created",
        issues=[{
            "number": 42,
            "title": "t",
            "url": "https://github.com/o/r/issues/42",
            "state": "open",
            "id": 900042,
            "attached": True,
        }],
        error=None,
    )
    assert payload.model_dump()["issues"] == [{
        "number": 42,
        "title": "t",
        "url": "https://github.com/o/r/issues/42",
        "state": "open",
        "plan_date": None,
        "complete_date": None,
        "estimate_hours": None,
    }]


def test_issue_state_wire_shape():
    payload = IssueStatePayload(
        ticket_id=1,
        model_name="helpdesk.ticket",
        number=42,
        state="closed",
        issues=[{"number": 42, "title": "t", "url": None, "state": "closed"}],
    )
    assert set(payload.model_dump()) == {"ticket_id", "model_name", "number", "state", "issues"}


def test_timesheet_results_wire_shape():
    payload = TimesheetResultsPayload(
        request_id="req-1",
        results=[{"line_id": 2, "status": "rewritten", "updated_desc": "x"}],
        stats={"total": 2, "ok": 1, "rewritten": 1, "needs_human": 0},
    )
    assert payload.model_dump() == {
        "request_id": "req-1",
        "results": [{
            "line_id": 2,
            "status": "rewritten",
            "updated_desc": "x",
            "reason": None,
        }],
        "stats": {"total": 2, "ok": 1, "rewritten": 1, "needs_human": 0},
    }


def test_contracts_table_complete_and_sane():
    names = {contract.name for contract in CONTRACTS}
    assert {
        "tickets.write-field",
        "tickets.reset-status",
        "tickets.issues-created",
        "tickets.issue-state",
        "hr.timesheet-results",
        "ticket-analysis",
        "create-issues",
        "timesheet-review",
        "health.response",
        "error",
    } <= names
    for contract in CONTRACTS:
        assert contract.direction in ("reva->odoo", "odoo->reva")
        assert contract.path.startswith("/") or contract.path == "*"
        assert contract.sample is not None


def test_callback_paths_are_namespaced():
    by_name = {contract.name: contract for contract in CONTRACTS}
    assert by_name["tickets.write-field"].path == "/tickets/write-field"
    assert by_name["tickets.issue-state"].path == "/tickets/issue-state"
    assert by_name["hr.timesheet-results"].path == "/hr/timesheet-results"


def test_every_sample_validates_against_its_model():
    for contract in CONTRACTS:
        if contract.model is not None:
            contract.model.model_validate(contract.sample)
            for extra in contract.extra_samples:
                contract.model.model_validate(extra)


def test_failed_issues_created_sample_exists():
    contracts = [contract for contract in CONTRACTS if contract.name == "tickets.issues-created"]
    assert any(
        sample.get("status") == "failed"
        for contract in contracts
        for sample in [contract.sample, *contract.extra_samples]
    )


def test_projection_keys_match_contract_models():
    """The wire projection tuples in odoo_client must track the contract
    models field-for-field — otherwise contracts/ advertises a field that
    _project_items silently strips from the wire (review finding #7)."""
    from reva.odoo_client import _ISSUE_KEYS, _TIMESHEET_RESULT_KEYS
    from reva.odoo_contracts import IssueRefPayload, TimesheetResultPayload

    assert set(_ISSUE_KEYS) == set(IssueRefPayload.model_fields)
    assert set(_TIMESHEET_RESULT_KEYS) == set(TimesheetResultPayload.model_fields)
