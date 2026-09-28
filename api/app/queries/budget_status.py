"""Data for the consultant-facing /reviews page (spec 2026-09-27): how full each
budget is and what is waiting for one. Read-only; nothing here echoes job args."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from rq.registry import ScheduledJobRegistry
from sqlalchemy import func, select

from app.queries.odoo_instances import list_odoo_instances
from app.settings import Settings
from reva.db import writers
from reva.db.engine import Database
from reva.db.models import PullRequest, Repository, ReviewRun

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


def build_status(db: Database, queue, settings: Settings) -> dict:
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
    authors = [
        {**a, "cap_usd": author_cap, "over": author_cap is not None and a["spent_usd"] >= author_cap}
        for a in review_cost_by_author_since(db, since)
    ]

    jobs: list[dict] = []
    jobs_error: str | None = None
    try:
        jobs = scheduled_budget_jobs(queue, db)
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
                "exhausted": bool(provider_credit_since),
                "since": min(provider_credit_since) if provider_credit_since else None,
            },
        },
        "waiting": {
            "reviews": reviews,
            "odoo": odoo,
            "jobs": jobs,
            "jobs_error": jobs_error,
        },
    }
