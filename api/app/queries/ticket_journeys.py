"""Ticket journey — read-only timeline over existing tables (spec 2026-07-10).

Review linkage is the documented v1 gap: reviews enter via change-note rows
(merged PRs) or persisted intent_check issue refs; open PRs without either
signal are invisible here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from reva.db import writers
from reva.db.engine import Database
from reva.db.models import (
    ChangeNote,
    PullRequest,
    Repository,
    ReviewRun,
    TicketAnalysis,
    TicketIssueRun,
)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def get_ticket_journey(
    db: Database, odoo_instance_id: int | None, model_name: str, ticket_id: int
) -> dict | None:
    events: list[dict] = []

    def _instance(col):
        return col.is_(None) if odoo_instance_id is None else col == odoo_instance_id

    with db.session() as s:
        analyses = s.execute(
            select(TicketAnalysis).where(
                TicketAnalysis.ticket_id == ticket_id,
                TicketAnalysis.model_name == model_name,
                _instance(TicketAnalysis.odoo_instance_id),
            )
        ).scalars().all()
        runs = s.execute(
            select(TicketIssueRun).where(
                TicketIssueRun.ticket_id == ticket_id,
                TicketIssueRun.model_name == model_name,
                _instance(TicketIssueRun.odoo_instance_id),
            )
        ).scalars().all()
        if not analyses and not runs:
            return None

        for a in analyses:
            events.append({"ts": a.created_at, "kind": "analysis_requested",
                           "summary": f"Analysis requested ({a.field_name})"})
            if a.status == "completed":
                cost = f", ${float(a.estimated_cost_usd):.2f}" if a.estimated_cost_usd else ""
                events.append({"ts": a.completed_at, "kind": "analysis_completed",
                               "summary": f"Analysis completed ({a.model or 'unknown model'}{cost})"})
            elif a.status == "failed":
                events.append({"ts": a.completed_at or a.created_at, "kind": "analysis_failed",
                               "summary": f"Analysis failed: {(a.error_message or 'unknown error')[:120]}"})

        repos: set[str] = set()
        for r in runs:
            if r.repo_full_name:
                repos.add(r.repo_full_name)
            if r.status == "completed" and r.issues:
                n = len(r.issues)
                total = sum(i.get("estimate_hours") or 0 for i in r.issues)
                bits = [f"{n} issue{'s' if n != 1 else ''}"]
                if r.parent_issue:
                    bits.append("+epic")
                if total:
                    bits.append(f"{total:.1f}h estimated")
                if r.github_project_url:
                    bits.append(f"board: {r.github_project_url}")
                events.append({"ts": r.created_at, "kind": "issues_created",
                               "summary": ", ".join(bits)})

        union = writers.get_ticket_issue_union(db, odoo_instance_id, ticket_id, model_name)
        union_numbers = {i["number"] for i in union}
        for item in union:
            if item.get("complete_date"):
                events.append({"ts": _parse_date(item["complete_date"]), "kind": "issue_closed",
                               "summary": f"#{item['number']} {item['title']} closed"})

        notes = s.execute(
            select(ChangeNote).where(
                ChangeNote.ticket_id == ticket_id,
                ChangeNote.model_name == model_name,
                _instance(ChangeNote.odoo_instance_id),
            )
        ).scalars().all()
        note_pairs = set()
        for cn in notes:
            note_pairs.add((cn.repo_full_name, cn.pr_number))
            events.append({"ts": cn.completed_at or cn.created_at, "kind": "change_note_posted",
                           "summary": f"{cn.repo_full_name}#{cn.pr_number} → internal note ({cn.status})"})

        seen_reviews: set[int] = set()
        if repos or note_pairs:
            review_rows = s.execute(
                select(ReviewRun, Repository.full_name, PullRequest.pr_number)
                .join(Repository, ReviewRun.repository_id == Repository.id)
                .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
                .where(
                    Repository.full_name.in_(repos | {p[0] for p in note_pairs}),
                    ReviewRun.status == "completed",
                )
            ).all()
            for rr, repo_name, pr_number in review_rows:
                linked = (repo_name, pr_number) in note_pairs
                if not linked and rr.intent_check:
                    linked = any(
                        v.get("issue_number") in union_numbers for v in rr.intent_check
                    )
                if not linked or rr.id in seen_reviews:
                    continue
                seen_reviews.add(rr.id)
                events.append({
                    "ts": rr.completed_at or rr.created_at, "kind": "review_completed",
                    "summary": f"{repo_name}#{pr_number} {rr.review_mode} review — "
                               f"risk {rr.risk_level or '?'}, {rr.finding_count} finding"
                               f"{'s' if rr.finding_count != 1 else ''}",
                })

    ready = bool(union) and all(i.get("state") == "closed" for i in union)
    if ready:
        parsed_closes = [_parse_date(i.get("complete_date")) for i in union]
        closes = [c for c in parsed_closes if c is not None]
        events.append({"ts": max(closes) if closes else None, "kind": "ready",
                       "summary": f"All {len(union)} issues closed"})

    def _sort_key(e: dict):
        ts = e["ts"]
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (ts is None, ts or datetime.max.replace(tzinfo=timezone.utc))

    events.sort(key=_sort_key)
    return {
        "ticket": {"odoo_instance_id": odoo_instance_id, "model_name": model_name,
                   "ticket_id": ticket_id, "ready": ready},
        "events": events,
    }
