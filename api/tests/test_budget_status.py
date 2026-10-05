"""Tests for the consultant-facing /reviews budget page and its JSON."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.queries.budget_status import explain_run
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import OpsEvent, PendingReview, ReviewRun
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
    app.state.rq_queue = SimpleNamespace(connection=object())
    monkeypatch.setattr("app.queries.budget_status.scheduled_budget_jobs", lambda queue, db: [])
    monkeypatch.setattr("app.queries.budget_status.Worker", SimpleNamespace(all=lambda connection: []))
    yield TestClient(app), db, monkeypatch
    app.dependency_overrides.clear()


def _seed_pr(db, *, author="alice", pr_number=42, repo_owner="acme", repo_name="w", state="open"):
    repo_id = writers.upsert_repository(db, github_repository_id=1000 + pr_number, owner=repo_owner,
                                        name=repo_name, default_branch="main", installation_id=5)
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=9000 + pr_number, pr_number=pr_number,
        title=f"PR {pr_number}", author_login=author, base_branch="main", head_branch="f",
        head_sha=f"sha{pr_number}", state=state, draft=False)
    return repo_id, pr_id


def _seed_run(db, *, repo_id, pr_id, head_sha, review_mode="diff", trigger_event="opened",
             status="completed", started_at=None, completed_at=None, finding_count=0,
             risk_level=None, decline_reason=None, error_message=None,
             budget_wait_since=None, budget_wait_reason=None, estimated_cost_usd=None,
             created_at=None):
    with db.session() as s:
        run = ReviewRun(
            repository_id=repo_id, pull_request_id=pr_id, head_sha=head_sha,
            status=status, trigger_event=trigger_event, review_mode=review_mode,
            started_at=started_at, completed_at=completed_at, finding_count=finding_count,
            risk_level=risk_level, decline_reason=decline_reason, error_message=error_message,
            budget_wait_since=budget_wait_since, budget_wait_reason=budget_wait_reason,
            estimated_cost_usd=estimated_cost_usd,
        )
        if created_at is not None:
            run.created_at = created_at
        s.add(run)
        s.flush()
        return run.id


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


def test_how_it_works_page_is_html(env):
    client, _, _ = env
    r = client.get("/reviews/how-it-works")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "/review-all" in r.text and ".claude-review.yml" in r.text
    assert client.get("/reviews/").text.count("how-it-works") >= 1   # nav link
    # both pages link to the docs browser and its internal-modules page
    for page in ("/reviews/", "/reviews/how-it-works"):
        html = client.get(page).text
        assert 'href="/docs/"' in html
        assert 'href="/docs/?page=internal-modules"' in html


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
    assert body["budgets"]["provider_credit"] == {
        "exhausted": False, "since": None, "last_refused_at": None, "last_paid_call_at": None}
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
                     "record": "helpdesk.ticket 6791", "budget_wait_since": odoo[0]["budget_wait_since"],
                     "reason": "cap"}]


def test_data_flags_provider_credit_exhausted_for_odoo_wait(env):
    """An Anthropic credit-balance wait flips the page's global banner flag,
    independent of which table the waiting row lives in."""
    client, db, _ = env
    inst = writers.create_odoo_instance(
        db, name="cu-prod", key_hash="h1", key_prefix="reva_odoo_aa",
        callback_url="", callback_api_key_enc="x",
    )
    aid = writers.record_ticket_analysis_created(db, TicketJobParams(
        analysis_id=0, odoo_instance_id=inst, ticket_id=6791, model_name="helpdesk.ticket",
        field_name="description", text="t"))
    writers.set_budget_wait_since(db, "ticket_analysis", aid, SINCE, reason="provider_credit")

    body = client.get("/reviews/data").json()

    assert body["budgets"]["provider_credit"] == {
        "exhausted": True, "since": body["waiting"]["odoo"][0]["budget_wait_since"],
        "last_refused_at": None, "last_paid_call_at": None,
    }
    assert body["waiting"]["odoo"][0]["reason"] == "provider_credit"


def test_data_provider_credit_not_exhausted_for_plain_cap_wait(env):
    client, db, _ = env
    _seed_review_waiting(db, pr_number=42)

    body = client.get("/reviews/data").json()

    assert body["budgets"]["provider_credit"] == {
        "exhausted": False, "since": None, "last_refused_at": None, "last_paid_call_at": None}
    assert body["waiting"]["reviews"][0]["prs"][0]["reason"] == "cap"


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
    # The registry failure short-circuits before workers_alive is computed, and
    # health.error reuses the same fixed text rather than a second ops event.
    assert body["health"]["workers_alive"] is None
    assert body["health"]["error"] == "job queue unreachable"
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
                                     "budget_wait_since": SINCE.isoformat(),
                                     "budget_wait_reason": "provider_credit"},)),
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
    # j1 carries no budget_wait_reason (pre-existing cap wait) -> defaults to
    # "cap"; j3 explicitly carries "provider_credit".
    assert out[0]["reason"] == "cap"
    assert out[1]["reason"] == "provider_credit"


def test_provider_credit_status_from_events(env):
    """Banner state comes from the refusal events vs the spend ledger, not only
    from waiting rows: refused after the last paid call -> exhausted; a paid
    call after the last refusal -> OK again."""
    client, db, _ = env
    body = client.get("/reviews/data").json()["budgets"]["provider_credit"]
    assert body == {"exhausted": False, "since": None, "last_refused_at": None, "last_paid_call_at": None}

    writers.record_ops_event(db, "review", "warning", "provider_credit_refused", {"task": "t"})
    body = client.get("/reviews/data").json()["budgets"]["provider_credit"]
    assert body["exhausted"] is True
    assert body["since"] is not None and body["last_refused_at"] is not None

    writers.record_claude_spend(db, "review", 0.5)   # a paid call proves the top-up
    body = client.get("/reviews/data").json()["budgets"]["provider_credit"]
    assert body["exhausted"] is False
    assert body["since"] is None and body["last_paid_call_at"] is not None


# --------------------------------------------------------- explain_run (2026-09-28)


def test_explain_run_table():
    cases = [
        (("completed", None, None, None, 3, "medium"),
         ("reviewed", "3 finding(s), risk medium")),
        (("running", None, None, None, 0, None),
         ("reviewing", "started; results land on the PR as a Check Run + review")),
        (("waiting_budget", None, None, "provider_credit", 0, None),
         ("waiting: Anthropic credit",
          "the account balance is empty; REVA re-checks hourly and resumes by itself")),
        (("waiting_budget", None, None, "cap", 0, None),
         ("waiting: your 24 h cap",
          "resumes automatically as your spend rolls off (see PR authors)")),
        (("declined", "No reviewable files under custom_addons/", None, None, 0, None),
         ("not reviewed: nothing under custom_addons/",
          "only files under custom_addons/ are reviewed; use /review-all for other paths")),
        (("declined", "Diff too large: 5000 lines", None, None, 0, None),
         ("not reviewed: diff too large",
          "split the PR, or raise max_diff_lines in .claude-review.yml")),
        (("declined", "review budget exhausted, wait expired", None, None, 0, None),
         ("declined: 24 h cap",
          "the cap was full and the wait expired; REVA retries by itself once "
          "the budget frees up; comment /review to retry sooner")),
        (("failed", None, "Credit balance is too low", None, 0, None),
         ("failed: Anthropic credit was empty",
          "REVA retries by itself once the balance is topped up; "
          "comment /review to retry sooner")),
        (("failed", None, "Anthropic credit balance too low for the maximum wait; "
          "review declined.", None, 0, None),
         ("failed: Anthropic credit was empty",
          "REVA retries by itself once the balance is topped up; "
          "comment /review to retry sooner")),
        (("failed", None, "boom, internal traceback", None, 0, None),
         ("failed", "internal error, ops were notified; comment /review to retry")),
        (("stale", None, None, None, 0, None),
         ("superseded", "a newer push replaced this commit")),
        (("skipped_trivial", None, None, None, 0, None),
         ("skipped: trivial change", "nothing worth a paid review")),
        (("queued", None, None, None, 0, None), ("queued", "")),
    ]
    for args, expected in cases:
        assert explain_run(*args) == expected

    long_reason = "some odd decline reason " * 10
    label, hint = explain_run("declined", long_reason, None, None, 0, None)
    assert label == "declined"
    assert hint == long_reason[:120]


# --------------------------------------------------------- activity.prs (2026-09-28)


def test_data_activity_prs_lists_latest_run_per_open_pr_newest_first(env):
    client, db, _ = env
    now = datetime.now(timezone.utc).replace(microsecond=0)
    repo_id, pr_id = _seed_pr(db, author="alice", pr_number=10)
    _seed_run(db, repo_id=repo_id, pr_id=pr_id, head_sha="sha10a", status="completed",
             started_at=now - timedelta(hours=2), completed_at=now - timedelta(hours=1, minutes=55),
             finding_count=1, risk_level="low", created_at=now - timedelta(hours=2))
    latest_id = _seed_run(db, repo_id=repo_id, pr_id=pr_id, head_sha="sha10b", status="completed",
                          started_at=now - timedelta(minutes=30), completed_at=now - timedelta(minutes=25),
                          finding_count=3, risk_level="high", created_at=now - timedelta(minutes=30))

    repo2_id, pr2_id = _seed_pr(db, author="bob", pr_number=11)
    _seed_run(db, repo_id=repo2_id, pr_id=pr2_id, head_sha="sha11", status="running",
             started_at=now - timedelta(minutes=5), created_at=now - timedelta(minutes=5))

    body = client.get("/reviews/data").json()
    prs = body["activity"]["prs"]
    assert [p["pr_number"] for p in prs] == [11, 10]   # newest run first

    row = next(p for p in prs if p["pr_number"] == 10)
    assert row["run_id"] == latest_id
    assert row["finding_count"] == 3 and row["risk_level"] == "high"
    assert row["pr_url"] == "https://github.com/acme/w/pull/10"
    assert row["label"] == "reviewed" and row["hint"] == "3 finding(s), risk high"

    running_row = next(p for p in prs if p["pr_number"] == 11)
    assert running_row["label"] == "reviewing"


def test_data_activity_prs_filters_by_author_case_insensitive_ignores_14_day_window(env):
    client, db, _ = env
    now = datetime.now(timezone.utc).replace(microsecond=0)
    repo_id, pr_id = _seed_pr(db, author="Alice", pr_number=20)
    old_run_id = _seed_run(db, repo_id=repo_id, pr_id=pr_id, head_sha="sha20", status="completed",
                           completed_at=now - timedelta(days=30), created_at=now - timedelta(days=30),
                           finding_count=0, risk_level="low")

    body = client.get("/reviews/data").json()
    assert body["activity"]["prs"] == []   # older than 14 days, no author filter

    body = client.get("/reviews/data?author=ALICE").json()
    prs = body["activity"]["prs"]
    assert len(prs) == 1
    assert prs[0]["run_id"] == old_run_id
    assert prs[0]["author_login"] == "Alice"


# ----------------------------------------------------------- activity.queue (2026-09-28)


def test_data_activity_queue_lists_unconsumed_pending_and_running(env):
    client, db, _ = env
    now = datetime.now(timezone.utc).replace(microsecond=0)
    repo_id, pr_id = _seed_pr(db, author="carol", pr_number=30)
    writers.upsert_pending_review(db, repository_id=repo_id, pull_request_id=pr_id, pr_number=30,
                                  head_sha="sha30", installation_id=5, trigger_event="opened",
                                  review_mode="diff", scheduled_at=now + timedelta(minutes=8))

    repo2_id, pr2_id = _seed_pr(db, author="dave", pr_number=31, repo_name="w2")
    consumed_id = writers.upsert_pending_review(db, repository_id=repo2_id, pull_request_id=pr2_id,
                                                pr_number=31, head_sha="sha31", installation_id=5,
                                                trigger_event="opened", review_mode="diff",
                                                scheduled_at=now)
    with db.session() as s:
        row = s.get(PendingReview, consumed_id)
        row.consumed = True

    _seed_run(db, repo_id=repo_id, pr_id=pr_id, head_sha="sha30b", status="running",
             started_at=now - timedelta(minutes=2), created_at=now - timedelta(minutes=2))

    body = client.get("/reviews/data").json()
    pending = body["activity"]["queue"]["pending"]
    running = body["activity"]["queue"]["running"]
    assert [p["pr_number"] for p in pending] == [30]
    assert pending[0]["pr_url"] == "https://github.com/acme/w/pull/30"
    assert pending[0]["review_mode"] == "diff" and pending[0]["trigger_event"] == "opened"
    assert [r["pr_number"] for r in running] == [30]
    assert running[0]["pr_url"] == "https://github.com/acme/w/pull/30"


# --------------------------------------------------------- budgets.authors.frees_at


def test_data_budgets_authors_frees_at_when_over_cap(env):
    client, db, _ = env
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="sqlite:///:memory:", github_app_id=1, github_webhook_secret="x",
        github_private_key="x", redis_url="redis://localhost:6379/0",
        daily_budget_usd=200.0, author_daily_budget_usd=1.0)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    t20 = now - timedelta(hours=20)
    t2 = now - timedelta(hours=2)
    repo_id, pr_id = _seed_pr(db, author="erin", pr_number=40)
    _seed_run(db, repo_id=repo_id, pr_id=pr_id, head_sha="sha40a", status="completed",
             completed_at=t20, created_at=t20, estimated_cost_usd=0.6)
    _seed_run(db, repo_id=repo_id, pr_id=pr_id, head_sha="sha40b", status="completed",
             completed_at=t2, created_at=t2, estimated_cost_usd=0.6)

    body = client.get("/reviews/data").json()
    erin = next(a for a in body["budgets"]["authors"] if a["author_login"] == "erin")
    assert erin["over"] is True
    assert erin["frees_at"] == (t20 + timedelta(hours=24)).isoformat()


def test_data_budgets_authors_frees_at_absent_when_under_cap(env):
    client, db, _ = env
    _seed_review_waiting(db, author="frank", pr_number=41, cost=1.0)

    body = client.get("/reviews/data").json()
    frank = next(a for a in body["budgets"]["authors"] if a["author_login"] == "frank")
    assert frank["over"] is False
    assert frank.get("frees_at") is None


# --------------------------------------------------------------------- health


def test_data_health_defaults_on_empty_db(env):
    client, _, _ = env
    body = client.get("/reviews/data").json()
    health = body["health"]
    assert health["last_webhook_at"] is None
    assert health["last_completed_review_at"] is None
    assert health["workers_alive"] == 0   # default-patched Worker.all() -> []
    assert health["error"] is None


def test_data_health_reports_last_webhook_and_completed_review(env):
    client, db, _ = env
    writers.record_github_event(db, delivery_id="d1", event_type="pull_request", action="opened",
                                repository_full_name="acme/w", sender_login="alice", payload={})
    repo_id, pr_id = _seed_pr(db, author="gina", pr_number=50)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _seed_run(db, repo_id=repo_id, pr_id=pr_id, head_sha="sha50", status="completed",
             completed_at=now, created_at=now)

    body = client.get("/reviews/data").json()
    assert body["health"]["last_webhook_at"] is not None
    assert body["health"]["last_completed_review_at"] == now.isoformat()


def test_data_health_workers_alive_from_patched_registry(env):
    client, _, monkeypatch = env
    monkeypatch.setattr("app.queries.budget_status.Worker",
                        SimpleNamespace(all=lambda connection: [object(), object(), object()]))
    body = client.get("/reviews/data").json()
    assert body["health"]["workers_alive"] == 3
