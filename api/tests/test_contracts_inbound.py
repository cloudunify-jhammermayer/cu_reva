"""Inbound contract samples must validate against the live FastAPI models."""

from __future__ import annotations

from app.schemas.support_requests import SupportRequestBody
from app.schemas.ticket_actuals import TicketActualsRequest
from app.schemas.ticket_analyses import TicketAnalysisRequest
from app.schemas.ticket_issues import CreateIssuesRequest, UpdateIssueEstimateRequest
from app.schemas.timesheet_reviews import TimesheetReviewRequest
from reva.odoo_contracts import CONTRACTS

_MODELS = {
    "support-request": SupportRequestBody,
    "ticket-analysis": TicketAnalysisRequest,
    "create-issues": CreateIssuesRequest,
    "update-issue-estimate": UpdateIssueEstimateRequest,
    "ticket-actuals": TicketActualsRequest,
    "timesheet-review": TimesheetReviewRequest,
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
