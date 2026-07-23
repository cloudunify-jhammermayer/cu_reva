"""Resolve PR closing references to REVA-created Odoo tickets."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select

from reva.db.engine import Database
from reva.db.models import OdooInstance, TicketAnalysis, TicketIssueRun

_CLOSING_REF_RE = re.compile(
    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TicketRef:
    odoo_instance_id: int
    ticket_id: int
    model_name: str
    run_id: int


def parse_closing_refs(text: str | None) -> list[int]:
    refs: list[int] = []
    for match in _CLOSING_REF_RE.finditer(text or ""):
        number = int(match.group(1))
        if number not in refs:
            refs.append(number)
    return refs


def resolve_pr_tickets(db: Database, repo_full_name: str, issue_numbers: list[int]) -> list[TicketRef]:
    if not issue_numbers:
        return []
    wanted = set(issue_numbers)
    repo = repo_full_name.lower()
    out: dict[tuple[int, int, str], TicketRef] = {}
    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.repo_full_name == repo,
                TicketIssueRun.issues.is_not(None),
            )
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
        ).scalars().all()
        for row in rows:
            if row.odoo_instance_id is None:
                continue
            numbers = {item.get("number") for item in (row.issues or [])}
            if not numbers.intersection(wanted):
                continue
            key = (row.odoo_instance_id, row.ticket_id, row.model_name)
            out.setdefault(key, TicketRef(
                odoo_instance_id=row.odoo_instance_id,
                ticket_id=row.ticket_id,
                model_name=row.model_name,
                run_id=row.id,
            ))
    return list(out.values())


# An optional leading `h` marks a helpdesk ticket (Odoo's `H1213` display-ref
# convention); a bare number is a project task. The type prefix (bug/feat/cr/…)
# is a work-item type, not an Odoo model — never mapped.
_TICKET_BRANCH_RE = re.compile(
    r"^(?:bug|feat|cr|conf|dev|mig|sup|doc)/(h)?(\d{1,9})$", re.IGNORECASE
)
# Trailing \b on the tag form (matching the token form's existing one) is
# required, not cosmetic: without it, capping the digit group at 9 chars
# would silently truncate a >9-digit run to a false 9-digit match instead of
# rejecting it outright.
_TICKET_TITLE_TAG_RE = re.compile(
    r"\[(?:bug|feat|cr|conf|dev|mig|sup|doc)\]\s*(h)?(\d{1,9})(?!\.\d)\b", re.IGNORECASE
)
_TICKET_TITLE_TOKEN_RE = re.compile(
    r"\b(?:bug|feat|cr|conf|dev|mig|sup|doc)/(h)?(\d{1,9})(?!\.\d)\b", re.IGNORECASE
)

# Model for an extracted ticket REVA has never seen (spec 2026-07-20, revised):
# a bare id defaults to project.task; an `H`-prefixed id is a helpdesk.ticket.
_HELPDESK_MODEL = "helpdesk.ticket"
_PROJECT_MODEL = "project.task"


def _github_url_matches_repo(github_url: str | None, repo: str) -> bool:
    """Boundary-checked owner/repo match — `acme/widgets` must not match
    `acme/widgets-legacy` (Task 2 review finding)."""
    url = (github_url or "").lower()
    idx = url.find("github.com/")
    if idx == -1:
        return False
    path = url[idx + len("github.com/"):].removesuffix(".git").rstrip("/")
    return path == repo or path.startswith(f"{repo}/")


def extract_ticket_id(head_branch: str | None, pr_title: str | None) -> tuple[int, str] | None:
    """(ticket_id, model_name) from the PR itself, for PRs with no linked REVA
    issue: the head branch (`cr/2010`, the convention ticket_issue_runner writes
    into issue bodies) first, then the PR title (`[CR] 2010 - …` tag form, then a
    `cr/2010` token). A bare number is a `project.task`; an `H`-prefixed id
    (`H1213`) is a `helpdesk.ticket`. None = no recognisable reference — normal
    lifecycle. Ids are bounded to 9 digits and 0 is rejected (never a real id)."""
    match = _TICKET_BRANCH_RE.match((head_branch or "").strip())
    if match:
        value = int(match.group(2))
        if value != 0:
            return value, _HELPDESK_MODEL if match.group(1) else _PROJECT_MODEL
    title = pr_title or ""
    match = _TICKET_TITLE_TAG_RE.search(title) or _TICKET_TITLE_TOKEN_RE.search(title)
    if match:
        value = int(match.group(2))
        if value != 0:
            return value, _HELPDESK_MODEL if match.group(1) else _PROJECT_MODEL
    return None


def resolve_ticket_by_id(
    db: Database, repo_full_name: str, ticket_id: int, default_model: str = _PROJECT_MODEL
) -> tuple[int, str] | None:
    """(odoo_instance_id, model_name) for an extracted ticket id.

    Ladder (spec 2026-07-20): ticket_issue_runs by (repo, ticket_id) newest
    first → ticket_analyses whose github_url matches the repo → ticket_analyses
    by id alone, newest first → the active is_default instance with
    `default_model` (extract_ticket_id's H-aware guess: project.task for a bare
    id, helpdesk.ticket for an `H`-prefixed one). A DB match wins over the guess
    — its recorded model is ground truth. None only when the ticket is unknown
    to REVA AND no active default instance exists (caller records the ops
    event)."""
    repo = repo_full_name.lower()
    with db.session() as s:
        row = s.execute(
            select(TicketIssueRun.odoo_instance_id, TicketIssueRun.model_name)
            .where(
                TicketIssueRun.repo_full_name == repo,
                TicketIssueRun.ticket_id == ticket_id,
                TicketIssueRun.odoo_instance_id.is_not(None),
            )
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
            .limit(1)
        ).first()
        if row is None:
            candidates = s.execute(
                select(
                    TicketAnalysis.odoo_instance_id,
                    TicketAnalysis.model_name,
                    TicketAnalysis.github_url,
                )
                .where(
                    TicketAnalysis.ticket_id == ticket_id,
                    TicketAnalysis.odoo_instance_id.is_not(None),
                )
                .order_by(TicketAnalysis.created_at.desc(), TicketAnalysis.id.desc())
            ).all()
            row = next(
                (c for c in candidates if _github_url_matches_repo(c.github_url, repo)),
                None,
            ) or (candidates[0] if candidates else None)
        if row is not None:
            return row.odoo_instance_id, row.model_name
        default_id = s.execute(
            select(OdooInstance.id)
            .where(OdooInstance.is_default.is_(True), OdooInstance.active.is_(True))
            .limit(1)
        ).scalar_one_or_none()
    return (default_id, default_model) if default_id is not None else None
