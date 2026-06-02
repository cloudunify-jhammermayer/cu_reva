"""Aggregation queries for the /api/v1/metrics/* endpoints.

Date truncation is dialect-aware so SQLite works in tests.
Postgres uses date_trunc(); SQLite uses strftime() as an approximation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Engine, case, func, select

from reva.db.engine import Database
from reva.db.models import PullRequest, Repository, ReviewFeedback, ReviewFinding, ReviewRun

_PERIOD_FMT = {
    "week": "%Y-W%W",
    "month": "%Y-%m",
    "quarter": "%Y-%m",  # SQLite fallback — groups by month, not quarter
}

_POSTGRES_PERIOD = {
    "week": "week",
    "month": "month",
    "quarter": "quarter",
}


def _dialect(engine: Engine) -> str:
    return engine.dialect.name


def _trunc_expr(col, period: str, dialect: str):
    """Return a SQLAlchemy expression that truncates a timestamp to a period."""
    if dialect == "sqlite":
        fmt = _PERIOD_FMT.get(period, "%Y-%m")
        return func.strftime(fmt, col)
    return func.date_trunc(_POSTGRES_PERIOD[period], col)


def _period_stats(s, since: datetime) -> dict:
    rows = s.execute(
        select(
            ReviewRun.status,
            func.count(ReviewRun.id).label("cnt"),
            func.avg(ReviewRun.duration_ms).label("avg_ms"),
        )
        .where(ReviewRun.created_at >= since)
        .where(ReviewRun.status.in_(["completed", "failed"]))
        .group_by(ReviewRun.status)
    ).all()

    completed = next((r.cnt for r in rows if r.status == "completed"), 0)
    failed = next((r.cnt for r in rows if r.status == "failed"), 0)
    avg_ms_completed = next((r.avg_ms for r in rows if r.status == "completed"), None)
    total = completed + failed
    success_rate = round(completed / total, 4) if total else 0.0
    return {
        "reviews_completed": completed,
        "reviews_failed": failed,
        "success_rate": success_rate,
        "avg_duration_ms": float(avg_ms_completed) if avg_ms_completed is not None else None,
    }


def _count_workers(redis_conn) -> int:
    # PERF-1: reuse the app's pooled Redis connection — don't open (and leak) a
    # fresh connection pool per dashboard request.
    if redis_conn is None:
        return 0
    try:
        from rq import Worker
        return len(Worker.all(connection=redis_conn))
    except Exception:
        return 0


def dashboard_metrics(db: Database, redis_conn=None) -> dict:
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)
    since_7d = now - timedelta(days=7)

    with db.session() as s:
        stats_24h = _period_stats(s, since_24h)
        stats_7d = _period_stats(s, since_7d)

        # Finding counts in last 24h (from completed reviews).
        finding_counts = s.execute(
            select(ReviewFinding.severity, func.count(ReviewFinding.id).label("cnt"))
            .join(ReviewRun, ReviewFinding.review_run_id == ReviewRun.id)
            .where(ReviewRun.created_at >= since_24h)
            .where(ReviewRun.status == "completed")
            .group_by(ReviewFinding.severity)
        ).all()
        fc = {r.severity: r.cnt for r in finding_counts}

        # Cost in last 7d.
        cost_row = s.execute(
            select(
                func.coalesce(func.sum(ReviewRun.estimated_cost_usd), 0).label("total"),
                func.count(ReviewRun.id).label("cnt"),
            )
            .where(ReviewRun.created_at >= since_7d)
            .where(ReviewRun.status == "completed")
        ).one()
        total_cost = float(cost_row.total)
        avg_cost = float(total_cost / cost_row.cnt) if cost_row.cnt else None

    return {
        "last_24h": stats_24h,
        "last_7d": stats_7d,
        "findings_24h": {
            "critical": fc.get("critical", 0),
            "major": fc.get("major", 0),
            "minor": fc.get("minor", 0),
            "info": fc.get("info", 0),
        },
        "total_cost_7d": total_cost,
        "avg_cost_per_review_7d": avg_cost,
        "active_workers": _count_workers(redis_conn),
    }


def developer_stats(db: Database, period: str) -> list[dict]:
    now = datetime.now(timezone.utc)
    # Use 8 weeks of data for trend calculation (4 recent + 4 prior).
    since = now - timedelta(weeks=8)
    recent_cutoff = now - timedelta(weeks=4)

    with db.session() as s:
        # Per-author stats for recent 4 weeks.
        recent = s.execute(
            select(
                PullRequest.author_login,
                func.count(ReviewRun.id).label("review_count"),
                func.avg(ReviewRun.finding_count).label("avg_findings"),
                func.avg(
                    case(
                        (ReviewFinding.severity.in_(["major", "critical"]), 1),
                        else_=0,
                    )
                ).label("avg_major_crit"),
            )
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .outerjoin(ReviewFinding, ReviewFinding.review_run_id == ReviewRun.id)
            .where(ReviewRun.status == "completed")
            .where(ReviewRun.created_at >= recent_cutoff)
            .group_by(PullRequest.author_login)
        ).all()

        # Per-author avg findings for prior 4 weeks (for trend).
        prior = s.execute(
            select(
                PullRequest.author_login,
                func.avg(ReviewRun.finding_count).label("avg_findings"),
            )
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .where(ReviewRun.status == "completed")
            .where(ReviewRun.created_at >= since)
            .where(ReviewRun.created_at < recent_cutoff)
            .group_by(PullRequest.author_login)
        ).all()

    prior_map = {r.author_login: float(r.avg_findings or 0) for r in prior}

    result = []
    for r in recent:
        if r.author_login is None:
            continue
        recent_avg = float(r.avg_findings or 0)
        prior_avg = prior_map.get(r.author_login)
        if prior_avg is None:
            trend = "stable"
        elif recent_avg < prior_avg * 0.8:
            trend = "improving"
        elif recent_avg > prior_avg * 1.2:
            trend = "worsening"
        else:
            trend = "stable"

        result.append(
            {
                "author_login": r.author_login,
                "review_count": r.review_count,
                "avg_findings": round(recent_avg, 2),
                "avg_major_critical": round(float(r.avg_major_crit or 0), 2),
                "trend": trend,
            }
        )

    return sorted(result, key=lambda x: x["review_count"], reverse=True)


def cost_stats(db: Database, period: str, repo: str | None) -> list[dict]:
    dialect = _dialect(db.engine)
    trunc = _trunc_expr(ReviewRun.created_at, period, dialect)

    with db.session() as s:
        q = (
            select(
                Repository.full_name.label("repo_full_name"),
                trunc.label("period"),
                func.sum(ReviewRun.estimated_cost_usd).label("total_cost"),
                func.count(ReviewRun.id).label("review_count"),
            )
            .join(Repository, ReviewRun.repository_id == Repository.id)
            .where(ReviewRun.status == "completed")
            .where(ReviewRun.estimated_cost_usd.isnot(None))
            .group_by(Repository.full_name, trunc)
            .order_by(trunc.desc(), Repository.full_name)
        )
        if repo:
            q = q.where(Repository.full_name == repo)

        rows = s.execute(q).all()

    return [
        {
            "repo_full_name": r.repo_full_name,
            "period": str(r.period),
            "total_cost_usd": float(r.total_cost),
            "review_count": r.review_count,
        }
        for r in rows
    ]


def feedback_stats(db: Database, since_days: int = 90) -> list[dict]:
    # PERF-5: bound the aggregation to a recent window so it doesn't scan the
    # whole (ever-growing) review_findings table on every dashboard load.
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    with db.session() as s:
        rows = s.execute(
            select(
                ReviewFinding.category,
                ReviewFinding.severity,
                func.count(
                    case((ReviewFeedback.is_positive == True, ReviewFeedback.id))  # noqa: E712
                ).label("thumbs_up"),
                func.count(
                    case((ReviewFeedback.is_positive == False, ReviewFeedback.id))  # noqa: E712
                ).label("thumbs_down"),
            )
            .outerjoin(ReviewFeedback, ReviewFeedback.review_finding_id == ReviewFinding.id)
            .where(ReviewFinding.created_at >= cutoff)
            .group_by(ReviewFinding.category, ReviewFinding.severity)
            .order_by(ReviewFinding.category, ReviewFinding.severity)
        ).all()

    result = []
    for r in rows:
        total = r.thumbs_up + r.thumbs_down
        approval = round(r.thumbs_up / total, 4) if total else None
        result.append(
            {
                "category": r.category,
                "severity": r.severity,
                "thumbs_up": r.thumbs_up,
                "thumbs_down": r.thumbs_down,
                "approval_rate": approval,
            }
        )
    return result
