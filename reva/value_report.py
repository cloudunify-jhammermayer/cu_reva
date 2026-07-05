"""Monthly value-report rollups."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from reva.db.engine import Database
from reva.db.models import (
    ClaudeSpend,
    OdooInstance,
    Repository,
    ReviewFinding,
    ReviewRun,
    TicketAnalysis,
    TicketIssueRun,
)


def build_report(db: Database, period_start: datetime, period_end: datetime) -> tuple[str, dict]:
    with db.session() as s:
        reviews = s.execute(
            select(func.count(ReviewRun.id), func.coalesce(func.sum(ReviewRun.estimated_cost_usd), 0))
            .where(ReviewRun.status == "completed")
            .where(ReviewRun.created_at >= period_start, ReviewRun.created_at < period_end)
        ).one()
        findings = s.execute(
            select(func.count(ReviewFinding.id))
            .join(ReviewRun, ReviewFinding.review_run_id == ReviewRun.id)
            .where(ReviewRun.status == "completed")
            .where(ReviewRun.created_at >= period_start, ReviewRun.created_at < period_end)
        ).scalar_one()
        resolved = s.execute(
            select(func.count(ReviewFinding.id))
            .join(ReviewRun, ReviewFinding.review_run_id == ReviewRun.id)
            .where(ReviewRun.status == "completed")
            .where(ReviewRun.created_at >= period_start, ReviewRun.created_at < period_end)
            .where(ReviewFinding.outcome == "resolved_by_fix")
        ).scalar_one()
        dismissed = s.execute(
            select(func.count(ReviewFinding.id))
            .join(ReviewRun, ReviewFinding.review_run_id == ReviewRun.id)
            .where(ReviewRun.status == "completed")
            .where(ReviewRun.created_at >= period_start, ReviewRun.created_at < period_end)
            .where(ReviewFinding.outcome == "dismissed")
        ).scalar_one()
        repo_rows = s.execute(
            select(
                Repository.full_name,
                func.count(ReviewRun.id),
                func.coalesce(func.sum(ReviewRun.estimated_cost_usd), 0),
            )
            .join(ReviewRun, ReviewRun.repository_id == Repository.id)
            .where(ReviewRun.status == "completed")
            .where(ReviewRun.created_at >= period_start, ReviewRun.created_at < period_end)
            .group_by(Repository.full_name)
            .order_by(Repository.full_name)
        ).all()
        instance_names = s.execute(
            select(OdooInstance.id, OdooInstance.name).order_by(OdooInstance.name)
        ).all()
        analysis_counts = dict(s.execute(
            select(TicketAnalysis.odoo_instance_id, func.count(TicketAnalysis.id))
            .where(TicketAnalysis.created_at >= period_start)
            .where(TicketAnalysis.created_at < period_end)
            .group_by(TicketAnalysis.odoo_instance_id)
        ).all())
        issue_run_counts = dict(s.execute(
            select(TicketIssueRun.odoo_instance_id, func.count(TicketIssueRun.id))
            .where(TicketIssueRun.created_at >= period_start)
            .where(TicketIssueRun.created_at < period_end)
            .group_by(TicketIssueRun.odoo_instance_id)
        ).all())
        spend_rows = s.execute(
            select(ClaudeSpend.kind, func.coalesce(func.sum(ClaudeSpend.cost_usd), 0))
            .where(ClaudeSpend.created_at >= period_start, ClaudeSpend.created_at < period_end)
            .group_by(ClaudeSpend.kind)
            .order_by(ClaudeSpend.kind)
        ).all()

    stats = {
        "reviews": int(reviews[0] or 0),
        "findings": int(findings or 0),
        "resolved_by_fix": int(resolved or 0),
        "dismissed": int(dismissed or 0),
        "spend_usd": float(reviews[1] or 0),
    }
    month = period_start.strftime("%Y-%m")
    lines = [f"# REVA value report {month}", "", "## Fleet"]
    if stats["reviews"] == 0:
        lines.append("No completed reviews in this period.")
    else:
        lines.append(
            f"- Completed reviews: {stats['reviews']}\n"
            f"- Findings: {stats['findings']} "
            f"({stats['resolved_by_fix']} resolved by fix, {stats['dismissed']} dismissed)\n"
            f"- Review spend: ${stats['spend_usd']:.2f}"
        )
    lines += ["", "## Repositories", "| Repository | Reviews | Spend |", "|---|---:|---:|"]
    for name, count, spend in repo_rows:
        lines.append(f"| {name} | {count} | ${float(spend or 0):.2f} |")
    lines += ["", "## Odoo instances", "| Instance | Analyses | Issue runs |", "|---|---:|---:|"]
    for instance_id, name in instance_names:
        lines.append(
            f"| {name} | {analysis_counts.get(instance_id, 0)} | "
            f"{issue_run_counts.get(instance_id, 0)} |"
        )
    lines += ["", "## Spend by kind", "| Kind | Spend |", "|---|---:|"]
    for kind, spend in spend_rows:
        lines.append(f"| {kind} | ${float(spend or 0):.2f} |")
    return "\n".join(lines) + "\n", stats
