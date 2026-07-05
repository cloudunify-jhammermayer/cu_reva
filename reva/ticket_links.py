"""Resolve PR closing references to REVA-created Odoo tickets."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select

from reva.db.engine import Database
from reva.db.models import TicketIssueRun

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
