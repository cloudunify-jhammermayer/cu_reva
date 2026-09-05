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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator


class WriteFieldPayload(BaseModel):
    ticket_id: int
    model_name: str
    field_name: str
    html: str
    # Support-answer metadata, split OUT of `html` so the consultant sees it in
    # the tab and the customer never does — the draft is sent verbatim through
    # the mail composer, so anything left in the body reaches them. Empty on
    # the ticket-analysis path, and older addons ignore the extra keys.
    answer_status: str = ""
    confidence: str = ""
    request_kind: str = ""
    # Rendered <ul> of the grounding REVA cited; "" when there was none.
    sources_html: str = ""
    # Correlation ids for Odoo's staleness guard — one per leg, whichever
    # produced this callback: `turn_id` on the support leg, `analysis_id` on
    # the analysis leg. 0 means "not sent": Odoo only enforces a non-zero id,
    # so a REVA that omits one keeps working, and neither leg can reject the
    # other's callback by matching a foreign id against its own stored value.
    turn_id: int = 0
    analysis_id: int = 0


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


class IssueWorkStatusItem(BaseModel):
    """Per-issue work-status hint. Odoo upserts by number (existing records
    only); `work_status` is a last-signal-wins display flag, not a state."""

    number: int
    work_status: Literal["in_progress", "in_review"]


class IssueWorkStatusPayload(BaseModel):
    """Two legs share this wire shape, never mixed in one call: the per-issue
    leg (issues non-empty — spec 2026-07-11) and the ticket-level leg
    (work_status + pr set, issues empty — the no-linked-issue PR fallback,
    spec 2026-07-20). Senders dump with exclude_none so the per-issue payload
    stays byte-identical to the pre-extension shape."""

    ticket_id: int
    model_name: str
    issues: list[IssueWorkStatusItem] = []
    work_status: Literal["in_progress", "in_review"] | None = None
    pr: PrRefPayload | None = None

    @model_validator(mode="after")
    def _one_leg_present(self) -> "IssueWorkStatusPayload":
        if not self.issues and self.work_status is None:
            raise ValueError(
                "either issues (per-issue leg) or work_status (ticket-level leg) is required"
            )
        return self


class ChangeSummaryNote(BaseModel):
    pr: PrRefPayload
    note_html: str


class ReleaseLogEntryPayload(BaseModel):
    """The ticket's entry in the repo's release log (docs/releases/<name>.md),
    rendered to simple HTML (p, ul, li, strong, em, code). Sent once per
    ticket on the change summary when the entry exists; the per-PR notes then
    carry an empty note_html (spec 2026-09-04-release-log-change-notes)."""

    release: str
    ticket: int
    title: str
    status: str
    modules: list[str]
    html: str


class ChangeSummaryPayload(BaseModel):
    """One consolidated merge summary delivered when the ticket flips ready —
    every undelivered per-PR change note batched into a single chatter post."""

    ticket_id: int
    model_name: str
    notes: list[ChangeSummaryNote]
    # Present when the repo's release log covers the ticket; omitted otherwise.
    release_log: ReleaseLogEntryPayload | None = None


class ReleaseNotePayload(BaseModel):
    """POST {base}/releases/release-note — the release-log lookup result
    (spec 2026-09-04-release-log-requirements, R2). `url`, `html` and `css` are
    set on `completed`; `error` on `failed`. Odoo composes html + css into one
    document and stores the URL as the "In REVA öffnen" link. None fields are
    omitted on the wire: a failed delivery carries only release_id, note_id,
    status and error, so the shipped Odoo model's `html: str = ""` default
    applies; url/css are optional there."""

    release_id: int
    note_id: int
    status: Literal["completed", "failed"]
    url: str | None = None
    html: str | None = None
    css: str | None = None
    error: str | None = None


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

    return {
        "support-request": SupportRequestBody,
        "ticket-analysis": TicketAnalysisRequest,
        "create-issues": CreateIssuesRequest,
        "update-issue-estimate": UpdateIssueEstimateRequest,
        "reassign-issue": ReassignIssueRequest,
        "ticket-actuals": TicketActualsRequest,
        "timesheet-review": TimesheetReviewRequest,
        "release-note": ReleaseNoteRequest,
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
            "analysis_id": 456,
        },
        # The support leg of the same endpoint: its own correlation id, the
        # metadata split out of the HTML, and analysis_id left at 0.
        extra_samples=[{
            "ticket_id": 123,
            "model_name": "project.task",
            "field_name": "reva_support_answer",
            "html": "<p>Der Rechnungslauf …</p>",
            "answer_status": "answered",
            "confidence": "high",
            "request_kind": "question",
            "sources_html": "<ul><li>account/models/account_move.py</li></ul>",
            "turn_id": 987,
        }],
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
        name="tickets.issue-work-status",
        direction="reva->odoo",
        method="POST",
        path="/tickets/issue-work-status",
        auth="bearer:instance-outbound-key",
        model=IssueWorkStatusPayload,
        sample={
            "ticket_id": 123,
            "model_name": "helpdesk.ticket",
            "issues": [{"number": 42, "work_status": "in_progress"}],
        },
        extra_samples=[{
            "ticket_id": 123,
            "model_name": "helpdesk.ticket",
            "issues": [],
            "work_status": "in_review",
            "pr": {"number": 42, "title": "Fix rounding",
                   "url": "https://github.com/acme/widgets/pull/42",
                   "repo": "acme/widgets"},
        }],
    ),
    Contract(
        name="tickets.change-summary",
        direction="reva->odoo",
        method="POST",
        path="/tickets/change-summary",
        auth="bearer:instance-outbound-key",
        model=ChangeSummaryPayload,
        sample={
            "ticket_id": 123,
            "model_name": "helpdesk.ticket",
            "notes": [{
                "pr": {
                    "number": 7,
                    "title": "Login rework",
                    "url": "https://github.com/acme/widgets/pull/7",
                    "repo": "acme/widgets",
                },
                "note_html": "",
            }],
            "release_log": {
                "release": "lollipop",
                "ticket": 123,
                "title": "Login rework",
                "status": "umgesetzt",
                "modules": ["cu_auth 19.0.1.0.0"],
                "html": "<p><strong>Gebaut</strong></p><p>Neue Anmeldung mit Rollenprüfung.</p>"
                "<p><strong>To-do</strong></p><ul><li>Rollen prüfen <em>(Einstellungen → Benutzer)</em></li></ul>",
            },
        },
        # Legacy shape (pre release-log): plain per-PR note_html, no release_log.
        extra_samples=[{
            "ticket_id": 123,
            "model_name": "helpdesk.ticket",
            "notes": [{
                "pr": {
                    "number": 7,
                    "title": "Login rework",
                    "url": "https://github.com/acme/widgets/pull/7",
                    "repo": "acme/widgets",
                },
                "note_html": "<p>Die Änderung wurde gemerged.</p>",
            }],
        }],
    ),
    Contract(
        name="releases.release-note",
        direction="reva->odoo",
        method="POST",
        path="/releases/release-note",
        auth="bearer:instance-outbound-key",
        model=ReleaseNotePayload,
        sample={
            "release_id": 3275,
            "note_id": 12,
            "status": "completed",
            "url": "https://reva.example.com/docs/?repo=4&path=docs/releases/lollipop.html",
            "html": '<div class="rl-page"><header class="rl-masthead"><h1>Lollipop</h1></header></div>',
            "css": ".rl-page{--rl-ground:#fbfbf9}",
        },
        extra_samples=[{
            "release_id": 3275,
            "note_id": 13,
            "status": "failed",
            "error": "Kein Release-Log 'docs/releases/lollipop.html' in acme/widgets, acme/other",
        }],
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
        name="support-request",
        direction="odoo->reva",
        method="POST",
        path="/api/v1/support-request",
        auth="bearer:instance-inbound-key",
        sample={
            "ticket_id": 4711,
            "model_name": "helpdesk.ticket",
            "field_name": "reva_support_answer",
            "thread_id": None,
            "subject": "Rechnungslauf bricht ab",
            "question": "Warum bricht der Rechnungslauf seit gestern ab?",
            "github_url": "https://github.com/acme/widgets",
            "persona_context": "Technisch versierter Kunde; kurz und sachlich.",
            "chatter": [
                {
                    "id": 98123,
                    "posted_at": "2026-07-25T09:14:00Z",
                    "author": "Maria Huber",
                    "author_kind": "customer",
                    "visibility": "public",
                    "body": "Seit dem Update bricht der Lauf ab.",
                },
                {
                    "id": 98124,
                    "posted_at": "2026-07-25T10:02:00Z",
                    "author": "Dev Team",
                    "author_kind": "internal",
                    "visibility": "internal",
                    "body": "Bekannt, Fix in 2.3, noch nicht deployed.",
                },
            ],
            "attachment": None,
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
            "github_url": "https://github.com/acme/widgets",
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
        extra_samples=[{
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
            "github_project_url": None,
            "plan_date": None,
            "release": {"id": 3275, "name": "Lollipop", "date": "2026-09-30 00:00:00"},
        }],
    ),
    Contract(
        name="update-issue-estimate",
        direction="odoo->reva",
        method="POST",
        path="/api/v1/update-issue-estimate",
        auth="bearer:instance-inbound-key",
        sample={
            "ticket_id": 42,
            "model_name": "project.task",
            "number": 42,
            "estimate_hours": 5.0,
        },
    ),
    Contract(
        name="reassign-issue",
        direction="odoo->reva",
        method="POST",
        path="/api/v1/reassign-issue",
        auth="bearer:instance-inbound-key",
        sample={
            "number": 42,
            "repo": "https://github.com/acme/widgets",
            "from": {"ticket_id": 1234, "model_name": "project.task"},
            "to": {"ticket_id": 5678, "model_name": "helpdesk.ticket"},
        },
    ),
    Contract(
        name="ticket-actuals",
        direction="odoo->reva",
        method="POST",
        path="/api/v1/ticket-actuals",
        auth="bearer:instance-inbound-key",
        sample={
            "ticket_id": 42,
            "model_name": "helpdesk.ticket",
            "actual_hours": 7.5,
            "timesheet_line_count": 4,
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
        name="release-note",
        direction="odoo->reva",
        method="POST",
        path="/api/v1/release-note",
        auth="bearer:instance-inbound-key",
        sample={
            "release_id": 3275,
            "name": "Lollipop",
            "date": "2026-09-30 00:00:00",
            "model_name": "project.task",
            "task_ids": [7595, 7620],
            "github_url": "https://github.com/acme/widgets",
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
