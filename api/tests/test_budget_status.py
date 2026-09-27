"""Tests for the consultant-facing /reviews budget page and its JSON."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import OpsEvent, ReviewRun
from reva.types import JobParams, TicketJobParams

SINCE = datetime.now(timezone.utc) - timedelta(hours=2)


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine_from_url("sqlite:///:memory:",
                                    connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(database_url="sqlite:///:memory:", github_app_id=1,
                        github_webhook_secret="x", github_private_key="x",
                        redis_url="redis://localhost:6379/0",
                        daily_budget_usd=200.0, author_daily_budget_usd=100.0)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.rq_queue = object()
    monkeypatch.setattr("app.queries.budget_status.scheduled_budget_jobs", lambda queue, db: [])
    yield TestClient(app), db, monkeypatch
    app.dependency_overrides.clear()


def _seed_review_waiting(db, *, author="alice", pr_number=42, cost=0.0):
    repo_id = writers.upsert_repository(db, github_repository_id=1, owner="acme", name="w",
                                        default_branch="main", installation_id=5)
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=9000 + pr_number, pr_number=pr_number,
        title=f"PR {pr_number}", author_login=author, base_branch="main", head_branch="f",
        head_sha=f"sha{pr_number}", state="open", draft=False)
    params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha=f"sha{pr_number}",
                       installation_id=5, review_mode="diff", trigger_event="opened")
    run_id = writers.record_review_waiting_budget(db, params, SINCE, "cap")
    if cost:
        with db.session() as s:
            run = s.get(ReviewRun, run_id)
            run.estimated_cost_usd = cost
            run.completed_at = datetime.now(timezone.utc)
    return run_id


def test_page_is_html(env):
    client, _, _ = env
    r = client.get("/reviews/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Waiting" in r.text


def test_data_empty_state(env):
    client, _, _ = env
    body = client.get("/reviews/data").json()
    assert body["settings"] == {"retry_seconds": 3600, "max_wait_seconds": 172800}
    assert body["budgets"]["global"] == {"spent_usd": 0.0, "cap_usd": 200.0, "over": False}
    assert body["budgets"]["instances"] == [] and body["budgets"]["authors"] == []
    assert body["waiting"] == {"reviews": [], "odoo": [], "jobs": [], "jobs_error": None}


def test_data_groups_waiting_reviews_per_repo_and_flags_over_cap_author(env):
    client, db, _ = env
    _seed_review_waiting(db, pr_number=42, cost=120.0)   # alice: over the $100 cap
    _seed_review_waiting(db, pr_number=43)
    writers.record_claude_spend(db, "audit", 250.0)       # global cap $200: over

    body = client.get("/reviews/data").json()

    assert body["budgets"]["global"]["over"] is True
    authors = body["budgets"]["authors"]
    assert authors[0]["author_login"] == "alice" and authors[0]["over"] is True
    groups = body["waiting"]["reviews"]
    assert len(groups) == 1
    assert groups[0]["repo_full_name"] == "acme/w"
    assert groups[0]["count"] == 2 and groups[0]["authors"] == ["alice"]
    assert [p["pr_number"] for p in groups[0]["prs"]] == [42, 43]


def test_data_lists_waiting_odoo_rows_with_instance_budget(env):
    client, db, _ = env
    inst = writers.create_odoo_instance(
        db, name="cu-prod", key_hash="h1", key_prefix="reva_odoo_aa",
        callback_url="", callback_api_key_enc="x",
    )
    writers.update_odoo_instance(db, inst, daily_budget_usd=60.0)
    aid = writers.record_ticket_analysis_created(db, TicketJobParams(
        analysis_id=0, odoo_instance_id=inst, ticket_id=6791, model_name="helpdesk.ticket",
        field_name="description", text="t"))
    writers.set_budget_wait_since(db, "ticket_analysis", aid, SINCE)

    body = client.get("/reviews/data").json()

    inst_row = body["budgets"]["instances"][0]
    assert inst_row["name"] == "cu-prod" and inst_row["cap_usd"] == 60.0
    odoo = body["waiting"]["odoo"]
    assert odoo == [{"kind": "ticket_analysis", "instance_name": "cu-prod",
                     "record": "helpdesk.ticket 6791", "budget_wait_since": odoo[0]["budget_wait_since"]}]


def test_data_includes_rowless_jobs_from_registry(env):
    client, _, monkeypatch = env
    monkeypatch.setattr("app.queries.budget_status.scheduled_budget_jobs", lambda queue, db: [
        {"kind": "audit", "target": "acme/w", "budget_wait_since": SINCE.isoformat(),
         "next_run_at": (SINCE + timedelta(minutes=15)).isoformat()},
    ])
    body = client.get("/reviews/data").json()
    assert body["waiting"]["jobs"][0]["kind"] == "audit"


def test_data_survives_registry_failure(env):
    """Review Focus 5: Redis down must not take the page down."""
    client, db, monkeypatch = env

    def boom(queue, db):
        raise ConnectionError("redis down")
    monkeypatch.setattr("app.queries.budget_status.scheduled_budget_jobs", boom)

    body = client.get("/reviews/data").json()

    assert body["waiting"]["jobs"] == []
    # Fixed, non-leaky text on the page; the real exception only reaches the
    # ops event (and the log).
    assert body["waiting"]["jobs_error"] == "job queue unreachable"
    with db.session() as s:
        events = [(e.component, e.event, e.detail) for e in s.query(OpsEvent).all()]
    assert any(
        c == "budget_status" and e == "scheduled_registry_unavailable"
        and "redis down" in (d or {}).get("error", "")
        for c, e, d in events
    )


def test_scheduled_budget_jobs_reads_registry(monkeypatch):
    """The real reader: only jobs carrying budget_wait_since, args never echoed."""
    from types import SimpleNamespace
    from app.queries import budget_status as bs

    jobs = {
        "j1": SimpleNamespace(func_name="worker.audit_tasks.run_audit",
                              args=({"repository_id": 7, "budget_wait_since": SINCE.isoformat()},)),
        "j2": SimpleNamespace(func_name="worker.tasks.run_comment_reply",
                              args=({"owner": "acme", "repo": "w", "pr_number": 3, "question": "secret?"},)),
        "j3": SimpleNamespace(func_name="worker.change_note_tasks.run_change_note",
                              args=({"repo_full_name": "acme/w", "pr_number": 9, "pr_body": "secret",
                                     "budget_wait_since": SINCE.isoformat()},)),
    }
    queue = SimpleNamespace(fetch_job=lambda jid: jobs[jid])
    fake_registry = SimpleNamespace(
        get_job_ids=lambda: list(jobs),
        get_scheduled_time=lambda jid: SINCE + timedelta(minutes=15),
    )
    monkeypatch.setattr(bs, "ScheduledJobRegistry", lambda queue: fake_registry)
    monkeypatch.setattr(bs, "_repo_full_name", lambda db, repo_id: "acme/w")

    out = bs.scheduled_budget_jobs(queue, db=object())

    assert [j["kind"] for j in out] == ["audit", "change_note"]   # j2 has no budget_wait_since
    assert out[0]["target"] == "acme/w" and out[1]["target"] == "acme/w #9"
    assert "secret" not in str(out)
