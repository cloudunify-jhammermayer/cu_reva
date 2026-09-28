"""Data for the consultant-facing /reviews page (spec 2026-09-27): how full each
budget is and what is waiting for one. Read-only; nothing here echoes job args."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from rq import Worker
from rq.registry import ScheduledJobRegistry
from sqlalchemy import func, select

from app.queries.odoo_instances import list_odoo_instances
from app.settings import Settings
from reva.db import writers
from reva.db.engine import Database
from reva.db.models import GithubEvent, PendingReview, PullRequest, Repository, ReviewRun

logger = structlog.get_logger()

# Deferred job types that have no DB row while waiting; read from RQ's
# scheduled registry instead. Row-backed kinds (reviews, Odoo work) come from
# the DB so a Redis flush cannot hide them.
ROWLESS_KINDS = {
    "worker.audit_tasks.run_audit": "audit",
    "worker.tasks.run_comment_reply": "comment_reply",
    "worker.change_note_tasks.run_change_note": "change_note",
}


def review_cost_by_author_since(db: Database, since: datetime) -> list[dict]:
    """Paid review spend per PR author in the window, highest first — the same
    rows sum_author_review_cost_since counts, grouped."""
    with db.session() as s:
        rows = s.execute(
            select(PullRequest.author_login, func.sum(ReviewRun.estimated_cost_usd))
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .where(ReviewRun.completed_at >= since, ReviewRun.estimated_cost_usd > 0,
                   PullRequest.author_login.is_not(None))
            .group_by(PullRequest.author_login)
            .order_by(func.sum(ReviewRun.estimated_cost_usd).desc())
        ).all()
    return [{"author_login": login, "spent_usd": round(float(total), 2)} for login, total in rows]


def list_author_review_costs_since(
    db: Database, author: str, since: datetime
) -> list[tuple[datetime, float]]:
    """Paid runs (completed_at, cost) for one author, oldest first — the same
    rows review_cost_by_author_since sums, walked to find when the rolling
    24 h cap next frees up."""
    with db.session() as s:
        rows = s.execute(
            select(ReviewRun.completed_at, ReviewRun.estimated_cost_usd)
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .where(ReviewRun.completed_at >= since, ReviewRun.estimated_cost_usd > 0,
                   PullRequest.author_login == author)
            .order_by(ReviewRun.completed_at.asc())
        ).all()
    return [(completed_at, float(cost)) for completed_at, cost in rows]


def _frees_at(cap: float, costs: list[tuple[datetime, float]]) -> str | None:
    """Earliest instant the rolling 24 h spend drops back under `cap`: walk the
    author's paid runs oldest-first subtracting cost from the running total
    until it clears the cap; that run's completed_at + 24h is when the oldest
    contributor rolls off. None if it never does within the given runs."""
    remaining = sum(cost for _, cost in costs)
    for completed_at, cost in costs:
        remaining -= cost
        if remaining < cap:
            return _iso(completed_at + timedelta(hours=24))
    return None


def explain_run(
    status: str,
    decline_reason: str | None,
    error_message: str | None,
    budget_wait_reason: str | None,
    finding_count: int,
    risk_level: str | None,
) -> tuple[str, str]:
    """Plain-language (label, hint) for a review run's status — the developer-
    facing sections on /reviews show this instead of raw status/decline_reason
    strings. Pure function; every branch is unit-tested."""
    if status == "completed":
        return "reviewed", f"{finding_count} finding(s), risk {risk_level}"
    if status == "running":
        return "reviewing", "started; results land on the PR as a Check Run + review"
    if status == "waiting_budget":
        if budget_wait_reason == "provider_credit":
            return ("waiting: Anthropic credit",
                    "the account balance is empty; REVA re-checks hourly and resumes by itself")
        return ("waiting: your 24 h cap",
                "resumes automatically as your spend rolls off (see PR authors)")
    if status == "declined":
        reason = decline_reason or ""
        if "No reviewable files" in reason:
            return ("not reviewed: nothing under custom_addons/",
                    "only files under custom_addons/ are reviewed; use /review-all for other paths")
        if "Diff too large" in reason:
            return ("not reviewed: diff too large",
                    "split the PR, or raise max_diff_lines in .claude-review.yml")
        if "review budget" in reason:
            return ("declined: 24 h cap",
                    "the cap was full and the wait expired; REVA retries by itself once "
                    "the budget frees up; comment /review to retry sooner")
        return "declined", reason[:120]
    if status == "failed":
        if error_message and ("anthropic credit balance too low" in error_message.lower() or "credit balance is too low" in error_message.lower()):
            return ("failed: Anthropic credit was empty",
                    "REVA retries by itself once the balance is topped up; "
                    "comment /review to retry sooner")
        return "failed", "internal error, ops were notified; comment /review to retry"
    if status == "stale":
        return "superseded", "a newer push replaced this commit"
    if status == "skipped_trivial":
        return "skipped: trivial change", "nothing worth a paid review"
    return status, ""


def list_recent_pr_runs(
    db: Database, *, author: str | None, since: datetime, limit: int = 100
) -> list[dict]:
    """Open PRs' latest review run each, newest first. Without an author
    filter, only runs from the last `since` (14 days on the page) qualify; an
    author filter drops that window and returns their full history instead."""
    with db.session() as s:
        latest_ids = (
            select(func.max(ReviewRun.id).label("id"))
            .group_by(ReviewRun.pull_request_id)
            .subquery()
        )
        query = (
            select(ReviewRun, Repository.full_name, PullRequest.pr_number, PullRequest.title,
                   PullRequest.author_login)
            .join(latest_ids, ReviewRun.id == latest_ids.c.id)
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .join(Repository, ReviewRun.repository_id == Repository.id)
            .where(PullRequest.state == "open")
        )
        if author:
            query = query.where(func.lower(PullRequest.author_login) == author.lower())
        else:
            query = query.where(ReviewRun.created_at >= since)
        rows = s.execute(query.order_by(ReviewRun.created_at.desc()).limit(limit)).all()

    out = []
    for rr, full_name, pr_number, title, author_login in rows:
        label, hint = explain_run(rr.status, rr.decline_reason, rr.error_message,
                                  rr.budget_wait_reason, rr.finding_count, rr.risk_level)
        out.append({
            "repo_full_name": full_name,
            "pr_number": pr_number,
            "pr_title": title,
            "author_login": author_login,
            "pr_url": f"https://github.com/{full_name}/pull/{pr_number}",
            "run_id": rr.id,
            "status": rr.status,
            "review_mode": rr.review_mode,
            "trigger_event": rr.trigger_event,
            "started_at": _iso(rr.started_at),
            "completed_at": _iso(rr.completed_at),
            "finding_count": rr.finding_count,
            "risk_level": rr.risk_level,
            "budget_wait_since": _iso(rr.budget_wait_since),
            "budget_wait_reason": rr.budget_wait_reason,
            "label": label,
            "hint": hint,
        })
    return out


def list_pending_queue(db: Database) -> list[dict]:
    """Unconsumed `pending_reviews` rows, oldest first — the debounce queue."""
    with db.session() as s:
        rows = s.execute(
            select(PendingReview, Repository.full_name)
            .join(Repository, PendingReview.repository_id == Repository.id)
            .where(PendingReview.consumed.is_(False))
            .order_by(PendingReview.scheduled_at)
        ).all()
    return [
        {"repo_full_name": full_name, "pr_number": pr.pr_number, "review_mode": pr.review_mode,
         "trigger_event": pr.trigger_event, "scheduled_at": _iso(pr.scheduled_at),
         "pr_url": f"https://github.com/{full_name}/pull/{pr.pr_number}"}
        for pr, full_name in rows
    ]


def list_running_queue(db: Database) -> list[dict]:
    """Review runs currently `running`."""
    with db.session() as s:
        rows = s.execute(
            select(ReviewRun, Repository.full_name, PullRequest.pr_number)
            .join(Repository, ReviewRun.repository_id == Repository.id)
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .where(ReviewRun.status == "running")
            .order_by(ReviewRun.started_at)
        ).all()
    return [
        {"repo_full_name": full_name, "pr_number": pr_number, "review_mode": rr.review_mode,
         "started_at": _iso(rr.started_at),
         "pr_url": f"https://github.com/{full_name}/pull/{pr_number}"}
        for rr, full_name, pr_number in rows
    ]


def _last_webhook_at(db: Database) -> datetime | None:
    with db.session() as s:
        return s.execute(select(func.max(GithubEvent.received_at))).scalar()


def _last_completed_review_at(db: Database) -> datetime | None:
    with db.session() as s:
        return s.execute(
            select(func.max(ReviewRun.completed_at)).where(ReviewRun.status == "completed")
        ).scalar()


def _repo_full_name(db: Database, repository_id: int) -> str:
    with db.session() as s:
        name = s.execute(select(Repository.full_name).where(Repository.id == repository_id)).scalar()
    return name or f"repo {repository_id}"


def _target(db: Database, kind: str, params: dict) -> str:
    if kind == "audit":
        return _repo_full_name(db, params.get("repository_id"))
    if kind == "comment_reply":
        return f"{params.get('owner')}/{params.get('repo')} #{params.get('pr_number')}"
    return f"{params.get('repo_full_name')} #{params.get('pr_number')}"


def scheduled_budget_jobs(queue, db: Database) -> list[dict]:
    """Deferred row-less jobs waiting in RQ's scheduled registry. `db` resolves
    an audit's repository id to its name; the test patches `_repo_full_name`."""
    registry = ScheduledJobRegistry(queue=queue)
    out: list[dict] = []
    for job_id in registry.get_job_ids():
        job = queue.fetch_job(job_id)
        if job is None:
            continue
        kind = ROWLESS_KINDS.get(job.func_name)
        params = job.args[0] if job.args and isinstance(job.args[0], dict) else {}
        if kind is None or params.get("budget_wait_since") is None:
            continue
        next_run = registry.get_scheduled_time(job_id)
        out.append({
            "kind": kind,
            "target": _target(db, kind, params),
            "budget_wait_since": params["budget_wait_since"],
            "reason": params.get("budget_wait_reason") or "cap",
            "next_run_at": next_run.isoformat() if next_run else None,
        })
    out.sort(key=lambda j: j["budget_wait_since"])
    return out


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _group_reviews(rows: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(r["repo_full_name"], {
            "repo_full_name": r["repo_full_name"], "count": 0, "authors": [],
            "oldest_since": None, "prs": [],
        })
        g["count"] += 1
        if r["author_login"] and r["author_login"] not in g["authors"]:
            g["authors"].append(r["author_login"])
        since = _iso(r["budget_wait_since"])
        if since and (g["oldest_since"] is None or since < g["oldest_since"]):
            g["oldest_since"] = since
        g["prs"].append({
            "id": r["id"], "pr_number": r["pr_number"], "pr_title": r["pr_title"],
            "author_login": r["author_login"], "review_mode": r["review_mode"],
            "budget_wait_since": since, "reason": r.get("budget_wait_reason") or "cap",
        })
    return [groups[k] for k in sorted(groups)]


def build_status(db: Database, queue, settings: Settings, *, author: str | None = None) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=1)
    global_spent = writers.sum_estimated_cost_since(db, since, exclude_kinds=writers.REVIEW_SPEND_KINDS)
    global_cap = settings.daily_budget_usd
    instances = []
    for inst in list_odoo_instances(db):
        spent = writers.sum_instance_cost_since(db, inst["id"], since)
        cap = inst["daily_budget_usd"]
        instances.append({"id": inst["id"], "name": inst["name"], "spent_usd": round(spent, 2),
                          "cap_usd": cap, "over": cap is not None and spent >= cap})
    author_cap = settings.author_daily_budget_usd
    authors = []
    for a in review_cost_by_author_since(db, since):
        over = author_cap is not None and a["spent_usd"] >= author_cap
        frees_at = None
        if over:
            costs = list_author_review_costs_since(db, a["author_login"], since)
            frees_at = _frees_at(author_cap, costs)
        authors.append({**a, "cap_usd": author_cap, "over": over, "frees_at": frees_at})

    jobs: list[dict] = []
    jobs_error: str | None = None
    workers_alive: int | None = None
    try:
        jobs = scheduled_budget_jobs(queue, db)
        workers_alive = len(Worker.all(connection=queue.connection))
    except Exception as exc:  # noqa: BLE001 — Redis down must not take the page down
        detail = str(exc)[:300]
        # Fixed, non-leaky text for the page; the real exception goes only to
        # the ops event and the log, not to whoever is looking at /reviews.
        jobs_error = "job queue unreachable"
        logger.warning("budget_status_registry_unavailable", error=detail)
        writers.record_ops_event(db, "budget_status", "warning", "scheduled_registry_unavailable",
                                 {"error": detail})

    reviews = _group_reviews(writers.list_reviews_waiting_budget(db))
    odoo = [
        {"kind": r["kind"], "instance_name": r["instance_name"], "record": r["record"],
         "budget_wait_since": _iso(r["budget_wait_since"]),
         "reason": r.get("budget_wait_reason") or "cap"}
        for r in writers.list_budget_waiting(db)
    ]

    # An empty Anthropic credit balance is not one of REVA's own spend caps —
    # every waiting row, whatever table it lives on, carries the same
    # provider_credit reason, so the banner is true iff any of them does.
    provider_credit_since: list[str] = []
    for g in reviews:
        for pr in g["prs"]:
            if pr["reason"] == "provider_credit" and pr["budget_wait_since"]:
                provider_credit_since.append(pr["budget_wait_since"])
    for row in odoo:
        if row["reason"] == "provider_credit" and row["budget_wait_since"]:
            provider_credit_since.append(row["budget_wait_since"])
    for job in jobs:
        if job.get("reason") == "provider_credit" and job["budget_wait_since"]:
            provider_credit_since.append(job["budget_wait_since"])

    # Persistent status, independent of waiting rows: the balance counts as
    # empty when the last provider_credit_refused event is newer than the last
    # paid call in the spend ledger (a paid call proves a top-up). Waiting rows
    # with that reason keep the banner on as well (older deployments, or a
    # refusal that came from a row the ledger can't see).
    last_refused = writers.latest_ops_event_at(db, "provider_credit_refused")
    last_paid = writers.latest_paid_call_at(db)
    refused_now = last_refused is not None and (last_paid is None or _iso(last_paid) < _iso(last_refused))
    streak_start = (
        writers.first_ops_event_at_after(db, "provider_credit_refused", last_paid)
        if refused_now else None
    )
    exhausted = refused_now or bool(provider_credit_since)
    since_candidates = [x for x in [_iso(streak_start), *provider_credit_since] if x]

    since_14d = datetime.now(timezone.utc) - timedelta(days=14)
    activity = {
        "prs": list_recent_pr_runs(db, author=author, since=since_14d),
        "queue": {"pending": list_pending_queue(db), "running": list_running_queue(db)},
    }
    health = {
        "last_webhook_at": _iso(_last_webhook_at(db)),
        "last_completed_review_at": _iso(_last_completed_review_at(db)),
        "workers_alive": workers_alive,
        "error": jobs_error,
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settings": {"retry_seconds": settings.budget_retry_seconds,
                     "max_wait_seconds": settings.budget_wait_max_seconds},
        "budgets": {
            "global": {"spent_usd": round(global_spent, 2), "cap_usd": global_cap,
                       "over": global_cap is not None and global_spent >= global_cap},
            "instances": instances,
            "authors": authors,
            "provider_credit": {
                "exhausted": exhausted,
                "since": min(since_candidates) if since_candidates else None,
                "last_refused_at": _iso(last_refused),
                "last_paid_call_at": _iso(last_paid),
            },
        },
        "activity": activity,
        "health": health,
        "waiting": {
            "reviews": reviews,
            "odoo": odoo,
            "jobs": jobs,
            "jobs_error": jobs_error,
        },
    }
