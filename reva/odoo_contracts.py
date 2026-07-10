"""Odoo<->REVA contract source of truth.

The outbound payload models are used at runtime by OdooCallbackClient, so the
generated contracts and the posted JSON cannot drift silently. The CONTRACTS
table drives the committed contracts/ artifacts and the coverage drift tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


class WriteFieldPayload(BaseModel):
    ticket_id: int
    model_name: str
    field_name: str
    html: str


class ResetStatusPayload(BaseModel):
    ticket_id: int
    model_name: str
    # Correlation id for Odoo's staleness guard: a reset whose analysis_id no
    # longer matches the record's stored one is a stale/replayed callback and
    # is rejected there with 409 (older addons ignore the extra key).
    analysis_id: int


class IssueRefPayload(BaseModel):
    """Documented per-issue shape; internal planning keys are stripped."""

    model_config = ConfigDict(extra="ignore")

    number: int | None = None
    title: str = ""
    url: str | None = None
    state: str | None = None
    # Per-issue dates (spec 2026-07-09), plain YYYY-MM-DD. plan_date echoes the
    # create-issues request; complete_date is GitHub's closed_at (cleared on
    # reopen). The Odoo addon .get()-reads and Date-truncates both.
    plan_date: str | None = None
    complete_date: str | None = None
    # Low-end AI-assisted dev estimate in hours (spec 2026-07-10). Omitted on
    # pre-rollout items; the Odoo addon .get()-reads it.
    estimate_hours: float | None = None


class IssuesCreatedPayload(BaseModel):
    ticket_id: int
    model_name: str
    request_id: int
    status: str
    issues: list[IssueRefPayload]
    error: str | None = None
    # Sum over union items carrying an estimate (children only — the epic is
    # parent_issue, never in the union). None when no item has an estimate.
    total_estimate_hours: float | None = None


class IssueStatePayload(BaseModel):
    ticket_id: int
    model_name: str
    number: int
    state: str
    issues: list[IssueRefPayload]


class TicketsReadyPayload(BaseModel):
    ticket_id: int
    model_name: str
    issues: list[IssueRefPayload]


class PrRefPayload(BaseModel):
    number: int
    title: str
    url: str
    repo: str


class ChangeNotePayload(BaseModel):
    ticket_id: int
    model_name: str
    pr: PrRefPayload
    note_html: str


class TimesheetResultPayload(BaseModel):
    line_id: int
    status: str
    updated_desc: str | None = None
    reason: str | None = None


class TimesheetResultsPayload(BaseModel):
    request_id: str
    results: list[TimesheetResultPayload]
    stats: dict[str, int]


@dataclass(frozen=True)
class Contract:
    name: str
    direction: str
    method: str
    path: str
    auth: str
    sample: dict[str, Any]
    model: type[BaseModel] | None = None
    extra_samples: list[dict[str, Any]] = field(default_factory=list)


def _inbound_models() -> dict[str, type[BaseModel]]:
    """Resolve FastAPI request models without making reva import app at runtime."""
    api_dir = str(Path(__file__).resolve().parents[1] / "api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    from app.schemas.ticket_analyses import TicketAnalysisRequest
    from app.schemas.ticket_issues import CreateIssuesRequest
    from app.schemas.timesheet_reviews import TimesheetReviewRequest

    return {
        "ticket-analysis": TicketAnalysisRequest,
        "create-issues": CreateIssuesRequest,
        "timesheet-review": TimesheetReviewRequest,
    }


_ISSUE_SAMPLE = {
    "number": 42,
    "title": "Implement login form",
    "url": "https://github.com/acme/widgets/issues/42",
    "state": "open",
    "plan_date": "2026-07-15",
    "complete_date": None,
    "estimate_hours": 3.5,
}

CONTRACTS: list[Contract] = [
    Contract(
        name="tickets.write-field",
        direction="reva->odoo",
        method="POST",
        path="/tickets/write-field",
        auth="bearer:instance-outbound-key",
        model=WriteFieldPayload,
        sample={
            "ticket_id": 123,
            "model_name": "helpdesk.ticket",
            "field_name": "x_reva_analysis",
            "html": "<h2>Summary</h2>",
        },
    ),
    Contract(
        name="tickets.reset-status",
        direction="reva->odoo",
        method="POST",
        path="/tickets/reset-status",
        auth="bearer:instance-outbound-key",
        model=ResetStatusPayload,
        sample={"ticket_id": 123, "model_name": "helpdesk.ticket", "analysis_id": 456},
    ),
    Contract(
        name="tickets.issues-created",
        direction="reva->odoo",
        method="POST",
        path="/tickets/issues-created",
        auth="bearer:instance-outbound-key",
        model=IssuesCreatedPayload,
        sample={
            "ticket_id": 123,
            "model_name": "helpdesk.ticket",
            "request_id": 7,
            "status": "created",
            "issues": [_ISSUE_SAMPLE],
            "error": None,
            "total_estimate_hours": 3.5,
        },
        extra_samples=[{
            "ticket_id": 123,
            "model_name": "helpdesk.ticket",
            "request_id": 7,
            "status": "failed",
            "issues": [],
            "error": "GitHub authentication failed",
            "total_estimate_hours": None,
        }],
    ),
    Contract(
        name="tickets.issue-state",
        direction="reva->odoo",
        method="POST",
        path="/tickets/issue-state",
        auth="bearer:instance-outbound-key",
        model=IssueStatePayload,
        sample={
            "ticket_id": 123,
            "model_name": "helpdesk.ticket",
            "number": 42,
            "state": "closed",
            "issues": [{**_ISSUE_SAMPLE, "state": "closed", "complete_date": "2026-07-09"}],
        },
    ),
    Contract(
        name="tickets.ready",
        direction="reva->odoo",
        method="POST",
        path="/tickets/ready",
        auth="bearer:instance-outbound-key",
        model=TicketsReadyPayload,
        sample={
            "ticket_id": 123,
            "model_name": "helpdesk.ticket",
            "issues": [{**_ISSUE_SAMPLE, "state": "closed", "complete_date": "2026-07-09"}],
        },
    ),
    Contract(
        name="tickets.change-note",
        direction="reva->odoo",
        method="POST",
        path="/tickets/change-note",
        auth="bearer:instance-outbound-key",
        model=ChangeNotePayload,
        sample={
            "ticket_id": 123,
            "model_name": "helpdesk.ticket",
            "pr": {
                "number": 7,
                "title": "Login rework",
                "url": "https://github.com/acme/widgets/pull/7",
                "repo": "acme/widgets",
            },
            "note_html": "<p>Die Änderung wurde gemerged.</p>",
        },
    ),
    Contract(
        name="hr.timesheet-results",
        direction="reva->odoo",
        method="POST",
        path="/hr/timesheet-results",
        auth="bearer:instance-outbound-key",
        model=TimesheetResultsPayload,
        sample={
            "request_id": "req-1",
            "results": [
                {
                    "line_id": 2,
                    "status": "rewritten",
                    "updated_desc": "Implemented reports",
                },
                {"line_id": 3, "status": "needs_human", "reason": "too thin"},
            ],
            "stats": {"total": 3, "ok": 1, "rewritten": 1, "needs_human": 1},
        },
    ),
    Contract(
        name="ticket-analysis",
        direction="odoo->reva",
        method="POST",
        path="/api/v1/ticket-analysis",
        auth="bearer:instance-inbound-key",
        sample={
            "ticket_id": 42,
            "model_name": "helpdesk.ticket",
            "field_name": "x_reva_analysis",
            "text": "The login page is broken.",
            "attachment": None,
        },
    ),
    Contract(
        name="create-issues",
        direction="odoo->reva",
        method="POST",
        path="/api/v1/create-issues",
        auth="bearer:instance-inbound-key",
        sample={
            "ticket_id": 42,
            "model_name": "project.task",
            "github_url": "https://github.com/acme/widgets",
            "name": "Login rework",
            "description": "Please add a login page.",
            "analysis_html": "",
            "description_docx": None,
            "priority": "1",
            "ticket_url": "https://odoo.example.com/web#id=42",
            "issue_type": None,
            "github_username": None,
            "github_project_url": "https://github.com/orgs/acme/projects/5",
            "plan_date": "2026-07-15",
        },
    ),
    Contract(
        name="timesheet-review",
        direction="odoo->reva",
        method="POST",
        path="/api/v1/timesheet-review",
        auth="bearer:instance-inbound-key",
        sample={
            "request_id": "req-1",
            "flagged_words": ["stupid"],
            "lines": [{
                "line_id": 1,
                "project_name": "ACME",
                "task_name": "Reports",
                "user_name": "Jo",
                "user_role": "developer",
                "description": "fixed stupid bug",
            }],
        },
    ),
    Contract(
        name="health.response",
        direction="odoo->reva",
        method="GET",
        path="/api/v1/health",
        auth="bearer:any",
        sample={"status": "ok", "authenticated_as": "instance", "instance": "acme"},
    ),
    Contract(
        name="error",
        direction="odoo->reva",
        method="*",
        path="*",
        auth="*",
        sample={"detail": "Odoo instance daily budget reached"},
    ),
]


def _schema_for(contract: Contract) -> dict[str, Any]:
    if contract.model is not None:
        return contract.model.model_json_schema()
    inbound = _inbound_models()
    if contract.name in inbound:
        return inbound[contract.name].model_json_schema()
    return {
        "type": "object",
        "properties": {key: {"type": _json_type(value)} for key, value in contract.sample.items()},
        "required": sorted(contract.sample.keys()),
        "additionalProperties": True,
    }


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return "string"


def _dump(path: Path, data: dict[str, Any]) -> bytes:
    raw = (json.dumps(data, indent=2, sort_keys=True, separators=(",", ": ")) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def generate(out_dir: Path) -> str:
    """Write contracts/ deterministically; return the contracts_version hash."""
    out_dir = Path(out_dir)
    hasher = hashlib.sha256()
    entries = []
    for contract in CONTRACTS:
        subdir = "callbacks" if contract.direction == "reva->odoo" else "inbound"
        schema_rel = f"{subdir}/{contract.name}.schema.json"
        sample_rel = f"{subdir}/{contract.name}.sample.json"
        hasher.update(_dump(out_dir / schema_rel, _schema_for(contract)))
        hasher.update(_dump(out_dir / sample_rel, contract.sample))
        for index, extra in enumerate(contract.extra_samples, start=2):
            hasher.update(_dump(out_dir / f"{subdir}/{contract.name}.sample{index}.json", extra))
        entries.append({
            "name": contract.name,
            "direction": contract.direction,
            "method": contract.method,
            "path": contract.path,
            "auth": contract.auth,
            "schema": schema_rel,
            "sample": sample_rel,
        })

    version = hasher.hexdigest()
    _dump(out_dir / "manifest.json", {
        "contracts_version": version,
        "generated_from": "reva/odoo_contracts.py",
        "contracts": entries,
    })
    return version


def check(committed_dir: Path) -> list[str]:
    """Regenerate into a temp dir and return changed/missing JSON paths."""
    committed_dir = Path(committed_dir)
    with tempfile.TemporaryDirectory() as tmp:
        fresh_dir = Path(tmp)
        generate(fresh_dir)
        fresh = {
            path.relative_to(fresh_dir): path.read_bytes()
            for path in fresh_dir.rglob("*.json")
        }
        existing = (
            {
                path.relative_to(committed_dir): path.read_bytes()
                for path in committed_dir.rglob("*.json")
            }
            if committed_dir.is_dir()
            else {}
        )
    return [
        str(rel)
        for rel in sorted(set(fresh) | set(existing))
        if fresh.get(rel) != existing.get(rel)
    ]


def _main() -> None:
    parser = argparse.ArgumentParser(prog="python -m reva.odoo_contracts")
    parser.add_argument("command", choices=["generate"])
    parser.add_argument("--out", default="contracts", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        problems = check(args.out)
        if problems:
            print("contracts/ is stale; regenerate and commit:")
            for problem in problems:
                print(f"  {problem}")
            raise SystemExit(1)
        print("contracts/ is current.")
        return
    version = generate(args.out)
    print(f"contracts_version {version} written to {args.out}/")


if __name__ == "__main__":
    _main()
