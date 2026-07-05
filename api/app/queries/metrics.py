"""Aggregation queries for the /api/v1/metrics/* endpoints.

Date truncation is dialect-aware so SQLite works in tests.
Postgres uses date_trunc(); SQLite uses strftime() as an approximation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Engine, case, func, select

from reva.db.engine import Database
from reva.db.models import (
    CoreKnowledgeVersion,
    MutedCategory,
    OpsEvent,
    PullRequest,
    RepoReviewMemory,
    Repository,
    ReviewFeedback,
    ReviewFinding,
    ReviewRun,
)

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
        degradations_24h = s.execute(
            select(func.count()).select_from(OpsEvent)
            .where(OpsEvent.created_at >= since_24h)
        ).scalar_one()
        core_rows = s.execute(
            select(CoreKnowledgeVersion).order_by(CoreKnowledgeVersion.odoo_version)
        ).scalars().all()
        core_knowledge = [
            {
                "odoo_version": row.odoo_version,
                "loaded_at": row.loaded_at,
                "modules": row.modules,
                "sections": row.sections,
            }
            for row in core_rows
        ]

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
        "degradations_24h": int(degradations_24h),
        "core_knowledge": core_knowledge,
    }


def developer_stats(db: Database, period: str) -> list[dict]:
    now = datetime.now(timezone.utc)
    # Use 8 weeks of data for trend calculation (4 recent + 4 prior).
    since = now - timedelta(weeks=8)
    recent_cutoff = now - timedelta(weeks=4)

    with db.session() as s:
        # CORR-3: review-grain stats must NOT join ReviewFinding, or the
        # outer-join row fan-out inflates review_count (counts findings) and
        # skews avg_findings. Keep this query at one-row-per-review.
        recent = s.execute(
            select(
                PullRequest.author_login,
                func.count(ReviewRun.id).label("review_count"),
                func.avg(ReviewRun.finding_count).label("avg_findings"),
            )
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .where(ReviewRun.status == "completed")
            .where(ReviewRun.created_at >= recent_cutoff)
            .group_by(PullRequest.author_login)
        ).all()

        # Finding-grain stat (separate query): fraction of this author's findings
        # that are major/critical — denominator is findings, which is correct.
        recent_major = s.execute(
            select(
                PullRequest.author_login,
                func.avg(
                    case(
                        (ReviewFinding.severity.in_(["major", "critical"]), 1),
                        else_=0,
                    )
                ).label("avg_major_crit"),
            )
            .select_from(ReviewRun)
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .join(ReviewFinding, ReviewFinding.review_run_id == ReviewRun.id)
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
    major_map = {r.author_login: float(r.avg_major_crit or 0) for r in recent_major}

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
                "avg_major_critical": round(major_map.get(r.author_login, 0.0), 2),
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


def learning_stats(db: Database, since_days: int = 90) -> list[dict]:
    """Per (repo, category): findings posted, how many a developer dismissed
    (negative feedback / `/dismiss`), and the fix outcomes. This is the input
    statistic for Tier-3 per-repo learned memory — a high dismiss rate in a
    category is the signal to suppress or down-weight it for that repo."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    with db.session() as s:
        rows = s.execute(
            select(
                Repository.full_name.label("repo"),
                ReviewFinding.category,
                func.count(func.distinct(ReviewFinding.id)).label("findings"),
                func.count(func.distinct(
                    case((ReviewFeedback.is_positive == False, ReviewFeedback.review_finding_id))  # noqa: E712
                )).label("dismissed"),
                func.count(func.distinct(
                    case((ReviewFinding.outcome == "resolved_by_fix", ReviewFinding.id))
                )).label("resolved_by_fix"),
                func.count(func.distinct(
                    case((ReviewFinding.outcome == "still_open_at_merge", ReviewFinding.id))
                )).label("still_open_at_merge"),
            )
            .select_from(ReviewFinding)
            .join(ReviewRun, ReviewFinding.review_run_id == ReviewRun.id)
            .join(Repository, ReviewRun.repository_id == Repository.id)
            .outerjoin(ReviewFeedback, ReviewFeedback.review_finding_id == ReviewFinding.id)
            .where(ReviewFinding.created_at >= cutoff)
            .group_by(Repository.full_name, ReviewFinding.category)
            .order_by(Repository.full_name, ReviewFinding.category)
        ).all()
    return [
        {
            "repo": r.repo,
            "category": r.category,
            "findings": r.findings,
            "dismissed": r.dismissed,
            "resolved_by_fix": r.resolved_by_fix,
            "still_open_at_merge": r.still_open_at_merge,
        }
        for r in rows
    ]


def active_mutes(db: Database) -> list[dict]:
    """Currently-muted (repo, category) pairs (Tier-3 /mute), newest first."""
    with db.session() as s:
        rows = s.execute(
            select(
                Repository.full_name.label("repo"),
                MutedCategory.category,
                MutedCategory.muted_by,
                MutedCategory.created_at,
            )
            .join(Repository, MutedCategory.repository_id == Repository.id)
            .where(MutedCategory.active.is_(True))
            .order_by(MutedCategory.created_at.desc())
        ).all()
    return [
        {"repo": r.repo, "category": r.category,
         "muted_by": r.muted_by, "created_at": r.created_at}
        for r in rows
    ]


def learned_memory(db: Database) -> list[dict]:
    """Active per-repo learned-memory block (Tier-3 feature B), newest first.
    Empty-content versions are omitted — nothing to show for those."""
    with db.session() as s:
        rows = s.execute(
            select(
                Repository.full_name.label("repo"),
                RepoReviewMemory.version,
                RepoReviewMemory.content,
                RepoReviewMemory.items,
                RepoReviewMemory.estimated_cost_usd,
                RepoReviewMemory.created_at,
            )
            .join(Repository, RepoReviewMemory.repository_id == Repository.id)
            .where(RepoReviewMemory.active.is_(True))
            .where(RepoReviewMemory.content != "")
            .order_by(RepoReviewMemory.created_at.desc())
        ).all()
    return [
        {
            "repo": r.repo,
            "version": r.version,
            "content": r.content,
            "item_count": len(r.items or []),
            "estimated_cost_usd": float(r.estimated_cost_usd) if r.estimated_cost_usd else None,
            "created_at": r.created_at,
        }
        for r in rows
    ]
