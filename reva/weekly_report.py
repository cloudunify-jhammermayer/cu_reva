"""Weekly report: query + Google Chat formatter.

`build_weekly_report(db, since)` returns a plain-text Google Chat message.
No HTTP here — the caller is responsible for sending it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select

from reva.db import writers
from reva.db.engine import Database
from reva.db.models import PullRequest, Repository, ReviewFinding, ReviewRun


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def weekly_report_stats(db: Database, since: datetime) -> dict:
    """Run all report queries in a single session and return a stats dict."""
    with db.session() as s:

        # 1. Per-status counts + duration + cost for completed reviews.
        status_rows = s.execute(
            select(
                ReviewRun.status,
                func.count(ReviewRun.id).label("cnt"),
                func.avg(ReviewRun.duration_ms).label("avg_ms"),
                func.min(ReviewRun.duration_ms).label("min_ms"),
                func.max(ReviewRun.duration_ms).label("max_ms"),
                func.sum(ReviewRun.estimated_cost_usd).label("total_cost"),
                func.avg(ReviewRun.estimated_cost_usd).label("avg_cost"),
            )
            .where(ReviewRun.created_at >= since)
            .group_by(ReviewRun.status)
        ).all()

        by_status: dict[str, dict] = {}
        for r in status_rows:
            by_status[r.status] = {
                "cnt": r.cnt,
                "avg_ms": float(r.avg_ms) if r.avg_ms is not None else None,
                "min_ms": float(r.min_ms) if r.min_ms is not None else None,
                "max_ms": float(r.max_ms) if r.max_ms is not None else None,
                "total_cost": float(r.total_cost) if r.total_cost is not None else 0.0,
                "avg_cost": float(r.avg_cost) if r.avg_cost is not None else None,
            }

        # 2. Findings by severity (from completed reviews only).
        sev_rows = s.execute(
            select(
                ReviewFinding.severity,
                func.count(ReviewFinding.id).label("cnt"),
            )
            .join(ReviewRun, ReviewFinding.review_run_id == ReviewRun.id)
            .where(ReviewRun.created_at >= since)
            .where(ReviewRun.status == "completed")
            .group_by(ReviewFinding.severity)
        ).all()
        findings_by_sev = {r.severity: r.cnt for r in sev_rows}

        # 3. Reviews per author (top 10, all statuses).
        author_rows = s.execute(
            select(
                PullRequest.author_login,
                func.count(ReviewRun.id).label("total"),
                func.sum(
                    case((ReviewRun.status == "completed", 1), else_=0)
                ).label("completed"),
            )
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .where(ReviewRun.created_at >= since)
            .where(PullRequest.author_login.isnot(None))
            .group_by(PullRequest.author_login)
            .order_by(func.count(ReviewRun.id).desc())
            .limit(10)
        ).all()
        authors = [
            {
                "login": r.author_login,
                "total": r.total,
                "completed": int(r.completed or 0),
            }
            for r in author_rows
        ]

        # 4. Top 5 recurring findings by title+severity.
        finding_rows = s.execute(
            select(
                ReviewFinding.title,
                ReviewFinding.severity,
                ReviewFinding.category,
                func.count(ReviewFinding.id).label("cnt"),
            )
            .join(ReviewRun, ReviewFinding.review_run_id == ReviewRun.id)
            .where(ReviewRun.created_at >= since)
            .where(ReviewRun.status == "completed")
            .group_by(ReviewFinding.title, ReviewFinding.severity, ReviewFinding.category)
            .order_by(func.count(ReviewFinding.id).desc())
            .limit(5)
        ).all()
        top_findings = [
            {
                "title": r.title,
                "severity": r.severity,
                "category": r.category,
                "count": r.cnt,
            }
            for r in finding_rows
        ]

        # 5. Per-repo review counts + cost (completed only).
        repo_rows = s.execute(
            select(
                Repository.full_name,
                func.count(ReviewRun.id).label("cnt"),
                func.sum(ReviewRun.estimated_cost_usd).label("cost"),
            )
            .join(Repository, ReviewRun.repository_id == Repository.id)
            .where(ReviewRun.created_at >= since)
            .where(ReviewRun.status == "completed")
            .group_by(Repository.full_name)
            .order_by(func.count(ReviewRun.id).desc())
        ).all()
        repos = [
            {
                "full_name": r.full_name,
                "count": r.cnt,
                "cost": float(r.cost) if r.cost is not None else 0.0,
            }
            for r in repo_rows
        ]

        # 6. Model usage.
        model_rows = s.execute(
            select(
                ReviewRun.model,
                func.count(ReviewRun.id).label("cnt"),
            )
            .where(ReviewRun.created_at >= since)
            .where(ReviewRun.status == "completed")
            .where(ReviewRun.model.isnot(None))
            .group_by(ReviewRun.model)
            .order_by(func.count(ReviewRun.id).desc())
        ).all()
        models = [{"model": r.model, "count": r.cnt} for r in model_rows]

    completed = by_status.get("completed", {})
    failed = by_status.get("failed", {})
    stale = by_status.get("stale", {})
    declined = by_status.get("declined", {})

    total_attempted = sum(v["cnt"] for v in by_status.values())
    total_completed = completed.get("cnt", 0)
    total_failed = failed.get("cnt", 0)
    success_rate = round(total_completed / total_attempted, 4) if total_attempted else 0.0

    return {
        "total_attempted": total_attempted,
        "completed": total_completed,
        "failed": total_failed,
        "stale": stale.get("cnt", 0),
        "declined": declined.get("cnt", 0),
        "success_rate": success_rate,
        "avg_duration_ms": completed.get("avg_ms"),
        "min_duration_ms": completed.get("min_ms"),
        "max_duration_ms": completed.get("max_ms"),
        "total_cost_usd": completed.get("total_cost", 0.0),
        "avg_cost_usd": completed.get("avg_cost"),
        "findings_by_severity": findings_by_sev,
        "authors": authors,
        "top_findings": top_findings,
        "repos": repos,
        "models": models,
        "ready_tickets": writers.list_ready_tickets(db, limit=10),
    }


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

_SEV_LABEL = {"critical": "Critical", "major": "Major", "minor": "Minor", "info": "Info"}


def _fmt_duration(ms: float | None) -> str:
    if ms is None:
        return "—"
    s = int(ms / 1000)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60:02d}s"


def _fmt_cost(usd: float | None) -> str:
    if usd is None:
        return "—"
    return f"${usd:.4f}"


def build_weekly_report(db: Database, since: datetime | None = None) -> str:
    """Return a formatted Google Chat message for the weekly report."""
    # CODE-8: read the clock once so the window start (when defaulted) and the
    # displayed period end are consistent.
    now = datetime.now(timezone.utc)
    if since is None:
        since = now - timedelta(days=7)

    stats = weekly_report_stats(db, since)

    period_end = now
    period_start = since
    date_range = (
        f"{period_start.strftime('%d %b')} – {period_end.strftime('%d %b %Y')}"
    )

    lines: list[str] = []
    lines.append(f"*REVA Weekly Report* — {date_range}")
    lines.append("")

    # --- Reviews ---
    lines.append("*Reviews*")
    sr = stats["success_rate"]
    lines.append(
        f"  Total: {stats['total_attempted']}   "
        f"Completed: {stats['completed']}   "
        f"Failed: {stats['failed']}   "
        f"Stale/Declined: {stats['stale'] + stats['declined']}"
    )
    lines.append(f"  Success rate: {sr:.1%}")
    lines.append("")

    # --- Findings ---
    fc = stats["findings_by_severity"]
    total_findings = sum(fc.values())
    if total_findings:
        lines.append(f"*Findings* ({total_findings} total from completed reviews)")
        sev_parts = []
        for sev in ("critical", "major", "minor", "info"):
            n = fc.get(sev, 0)
            if n:
                sev_parts.append(f"{_SEV_LABEL[sev]}: {n}")
        lines.append("  " + "   ".join(sev_parts))
        lines.append("")

    # --- Performance ---
    lines.append("*Performance*")
    lines.append(
        f"  Avg: {_fmt_duration(stats['avg_duration_ms'])}   "
        f"Min: {_fmt_duration(stats['min_duration_ms'])}   "
        f"Max: {_fmt_duration(stats['max_duration_ms'])}"
    )
    lines.append("")

    # --- Cost ---
    lines.append("*Cost (completed reviews)*")
    lines.append(
        f"  Total: {_fmt_cost(stats['total_cost_usd'])}   "
        f"Per review: {_fmt_cost(stats['avg_cost_usd'])}"
    )
    lines.append("")

    # --- By repo ---
    if stats["repos"]:
        lines.append("*By repository*")
        for r in stats["repos"]:
            lines.append(f"  `{r['full_name']}`: {r['count']} reviews  ({_fmt_cost(r['cost'])})")
        lines.append("")

    # --- By author (top 10) ---
    if stats["authors"]:
        lines.append("*Reviews by author*")
        for a in stats["authors"]:
            lines.append(f"  @{a['login']}: {a['completed']}/{a['total']}")
        lines.append("")

    # --- Top findings ---
    if stats["top_findings"]:
        lines.append("*Top recurring findings*")
        for f in stats["top_findings"]:
            label = _SEV_LABEL.get(f["severity"], f["severity"].capitalize())
            lines.append(f"  {f['title']}  ×{f['count']}  `[{label}]`")
        lines.append("")

    # --- Model usage ---
    if stats["models"]:
        model_parts = [f"{m['model']}: {m['count']}" for m in stats["models"]]
        lines.append(f"*Models*  {' · '.join(model_parts)}")
        lines.append("")

    # --- Ready tickets ---
    if stats["ready_tickets"]:
        lines.append("*Ready for deployment*")
        for ticket in stats["ready_tickets"]:
            repo = ticket.get("repo_full_name") or "(no repo)"
            lines.append(
                f"  `{repo}` ticket {ticket['ticket_id']} "
                f"({ticket['issue_count']} issues closed)"
            )
        lines.append("")

    lines.append("_REVA_")

    return "\n".join(lines)
