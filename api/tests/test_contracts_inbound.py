"""Inbound contract samples must validate against the live FastAPI models."""

from __future__ import annotations

from app.schemas.release_notes import ReleaseNoteRequest
from app.schemas.support_requests import SupportRequestBody
from app.schemas.ticket_actuals import TicketActualsRequest
from app.schemas.ticket_analyses import TicketAnalysisRequest
from app.schemas.ticket_issues import (
    CreateIssuesRequest,
    ReassignIssueRequest,
    UpdateIssueEstimateRequest,
)
from app.schemas.timesheet_reviews import TimesheetReviewRequest
from reva.odoo_contracts import CONTRACTS

_MODELS = {
    "support-request": SupportRequestBody,
    "ticket-analysis": TicketAnalysisRequest,
    "create-issues": CreateIssuesRequest,
    "update-issue-estimate": UpdateIssueEstimateRequest,
    "reassign-issue": ReassignIssueRequest,
    "ticket-actuals": TicketActualsRequest,
    "timesheet-review": TimesheetReviewRequest,
    "release-note": ReleaseNoteRequest,
}


def test_inbound_samples_validate():
    for contract in CONTRACTS:
        model = _MODELS.get(contract.name)
        if model is None:
            continue
        model.model_validate(contract.sample)
        for extra in contract.extra_samples:
            model.model_validate(extra)


def test_all_inbound_request_contracts_covered():
    inbound = {
        contract.name
        for contract in CONTRACTS
        if contract.direction == "odoo->reva" and contract.method == "POST"
    }
    assert inbound == set(_MODELS), (
        "new inbound create route? add its model here and a CONTRACTS entry"
    )


def test_create_issues_release_block_is_typed():
    from reva.types import ReleaseRef

    body = CreateIssuesRequest.model_validate({
        "ticket_id": 42, "model_name": "project.task",
        "github_url": "https://github.com/acme/widgets", "name": "Login rework",
        "description": "Please add a login page.", "analysis_html": "", "priority": "1",
        "ticket_url": "https://odoo.example.com/web#id=42",
        "release": {"id": 3275, "name": "Lollipop", "date": "2026-09-30 00:00:00"},
    })
    assert body.release == ReleaseRef(id=3275, name="Lollipop", date="2026-09-30 00:00:00")
