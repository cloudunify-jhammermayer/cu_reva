"""Tests for GET /api/v1/ticket-journeys (per-ticket timeline, spec 2026-07-10)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import TicketAnalysis, TicketIssueRun
from reva.types import Finding, IntentIssueVerdict, JobParams, ReviewResult


# --- fixture ------------------------------------------------------------------


@pytest.fixture()
def client_and_db():
    engine = create_engine_from_url(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(
        database_url="sqlite:///:memory:",
        github_app_id=1,
        github_webhook_secret="x",
        github_private_key="x",
        redis_url="redis://localhost:6379/0",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app), db
    app.dependency_overrides.clear()


# --- seed helpers -------------------------------------------------------------


def _add_analysis(
    db, *, odoo_instance_id, ticket_id, model_name="helpdesk.ticket",
    field_name="x_reva_analysis", status="completed", error_message=None,
    created_at=None, completed_at=None,
) -> None:
    kwargs = dict(
        odoo_instance_id=odoo_instance_id, ticket_id=ticket_id, model_name=model_name,
        field_name=field_name, input_text="t", status=status, error_message=error_message,
    )
    if created_at is not None:
        kwargs["created_at"] = created_at
    if completed_at is not None:
        kwargs["completed_at"] = completed_at
    with db.session() as s:
        s.add(TicketAnalysis(**kwargs))


def _add_issue_run(
    db, *, odoo_instance_id, ticket_id, model_name="helpdesk.ticket",
    repo_full_name="acme/widgets", status="pending", issues=None,
    parent_issue=None, github_project_url=None, created_at=None,
) -> int:
    kwargs = dict(
        odoo_instance_id=odoo_instance_id, ticket_id=ticket_id, model_name=model_name,
        github_url=f"https://github.com/{repo_full_name}", repo_full_name=repo_full_name,
        name="Ticket", description="d", analysis_html="<p>a</p>", priority="1",
        ticket_url="https://odoo.example.com/1", status=status, issues=issues,
        parent_issue=parent_issue, github_project_url=github_project_url,
    )
    if created_at is not None:
        kwargs["created_at"] = created_at
    with db.session() as s:
        row = TicketIssueRun(**kwargs)
        s.add(row)
        s.flush()
        return row.id


def _seed_repo_and_pr(db, *, repo_num=1001, pr_num=42) -> tuple[int, int]:
    repo_id = writers.upsert_repository(
        db, github_repository_id=repo_num, owner="acme", name="widgets",
        default_branch="main", installation_id=99,
    )
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=5000 + pr_num, pr_number=pr_num,
        title=f"PR #{pr_num}", author_login="alice", base_branch="main",
        head_branch="feat/x", head_sha="deadbeef", state="open", draft=False,
    )
    return repo_id, pr_id


def _seed_review(
    db, *, repo_id: int, pr_id: int, status: str = "completed", finding_count: int = 0,
    intent_check: list[dict] | None = None,
) -> int:
    params = JobParams(
        repository_id=repo_id, pull_request_id=pr_id,
        head_sha="deadbeef", installation_id=99,
        review_mode="diff", trigger_event="opened",
    )
    rr_id = writers.record_review_started(db, params)
    if status == "completed":
        findings = [
            Finding(severity="major", category="bug", title=f"Bug {i}", body="details",
                    confidence=0.9)
            for i in range(finding_count)
        ]
        result = ReviewResult(
            status="completed", summary="Looks OK",
            findings=findings, risk_level="low",
            intent_check=(
                [IntentIssueVerdict(**v) for v in intent_check] if intent_check else None
            ),
        )
        writers.record_review_completed(db, params, result)
    return rr_id


# --- tests --------------------------------------------------------------------


def test_journey_404_for_unknown_ticket(client_and_db):
    client, _ = client_and_db
    resp = client.get("/api/v1/ticket-journeys?model_name=helpdesk.ticket&ticket_id=1&odoo_instance_id=1")
    assert resp.status_code == 404


def test_journey_analyses_only(client_and_db):
    client, db = client_and_db
    _add_analysis(db, odoo_instance_id=1, ticket_id=4711, status="completed")

    resp = client.get("/api/v1/ticket-journeys?model_name=helpdesk.ticket&ticket_id=4711&odoo_instance_id=1")
    assert resp.status_code == 200
    data = resp.json()
    kinds = [e["kind"] for e in data["events"]]
    assert kinds == ["analysis_requested", "analysis_completed"]
    assert data["ticket"]["ready"] is False


def test_journey_failed_analysis_event(client_and_db):
    client, db = client_and_db
    long_error = "E" * 200
    _add_analysis(db, odoo_instance_id=1, ticket_id=4712, status="failed", error_message=long_error)

    resp = client.get("/api/v1/ticket-journeys?model_name=helpdesk.ticket&ticket_id=4712&odoo_instance_id=1")
    assert resp.status_code == 200
    data = resp.json()
    kinds = [e["kind"] for e in data["events"]]
    assert kinds == ["analysis_requested", "analysis_failed"]
    failed_summary = data["events"][1]["summary"]
    assert failed_summary == f"Analysis failed: {'E' * 120}"
    assert len(failed_summary) < len(f"Analysis failed: {long_error}")


def test_journey_issues_and_closes_and_ready(client_and_db):
    client, db = client_and_db
    issues = [
        {"number": 1, "title": "Issue A", "url": "https://github.com/acme/widgets/issues/1",
         "state": "closed", "complete_date": "2026-07-08", "estimate_hours": 1.5},
        {"number": 2, "title": "Issue B", "url": "https://github.com/acme/widgets/issues/2",
         "state": "closed", "complete_date": "2026-07-09", "estimate_hours": 1.5},
    ]
    _add_issue_run(
        db, odoo_instance_id=1, ticket_id=4713, status="completed", issues=issues,
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    resp = client.get("/api/v1/ticket-journeys?model_name=helpdesk.ticket&ticket_id=4713&odoo_instance_id=1")
    assert resp.status_code == 200
    data = resp.json()
    kinds = [e["kind"] for e in data["events"]]
    assert kinds == ["issues_created", "issue_closed", "issue_closed", "ready"]
    assert "2 issues" in data["events"][0]["summary"]
    assert "3.0h" in data["events"][0]["summary"]
    ready_event = data["events"][-1]
    assert datetime.fromisoformat(ready_event["ts"]).date().isoformat() == "2026-07-09"
    assert data["ticket"]["ready"] is True


def test_journey_change_note_links_review(client_and_db):
    client, db = client_and_db
    _add_issue_run(db, odoo_instance_id=1, ticket_id=4714, repo_full_name="acme/widgets")
    note_id, _ = writers.get_or_create_change_note(
        db, repo_full_name="acme/widgets", pr_number=88, ticket_id=4714,
        odoo_instance_id=1, model_name="helpdesk.ticket",
    )
    writers.record_change_note_completed(db, note_id, "<p>note</p>", 0.01)

    repo_id, pr_id = _seed_repo_and_pr(db, repo_num=2001, pr_num=88)
    _seed_review(db, repo_id=repo_id, pr_id=pr_id)

    resp = client.get("/api/v1/ticket-journeys?model_name=helpdesk.ticket&ticket_id=4714&odoo_instance_id=1")
    assert resp.status_code == 200
    data = resp.json()
    kinds = [e["kind"] for e in data["events"]]
    assert "review_completed" in kinds
    assert "change_note_posted" in kinds
    review_event = next(e for e in data["events"] if e["kind"] == "review_completed")
    assert "acme/widgets#88" in review_event["summary"]


def test_journey_intent_check_links_review(client_and_db):
    client, db = client_and_db
    _add_issue_run(
        db, odoo_instance_id=1, ticket_id=4715, repo_full_name="acme/widgets",
        status="completed",
        issues=[{"number": 42, "title": "Issue #42",
                 "url": "https://github.com/acme/widgets/issues/42", "state": "open"}],
    )
    repo_id, pr_id_90 = _seed_repo_and_pr(db, repo_num=2002, pr_num=90)
    _seed_review(db, repo_id=repo_id, pr_id=pr_id_90,
                 intent_check=[{"issue_number": 42, "verdict": "matches", "note": "ok"}])

    # Unrelated review on the same repo citing an unrelated issue -> must not link.
    _, pr_id_91 = _seed_repo_and_pr(db, repo_num=2002, pr_num=91)
    _seed_review(db, repo_id=repo_id, pr_id=pr_id_91,
                 intent_check=[{"issue_number": 999, "verdict": "matches", "note": "n/a"}])

    resp = client.get("/api/v1/ticket-journeys?model_name=helpdesk.ticket&ticket_id=4715&odoo_instance_id=1")
    assert resp.status_code == 200
    kinds = [e["kind"] for e in resp.json()["events"]]
    assert kinds.count("review_completed") == 1


def test_journey_orders_by_ts_nulls_last(client_and_db):
    client, db = client_and_db
    _add_analysis(
        db, odoo_instance_id=1, ticket_id=4716, field_name="a", status="completed",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        completed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    # An analysis with no completed_at/failed status yields only analysis_requested.
    _add_analysis(
        db, odoo_instance_id=1, ticket_id=4716, field_name="b", status="pending",
        created_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )
    # A ready ticket whose complete_date can't be parsed yields a null ts.
    _add_issue_run(
        db, odoo_instance_id=1, ticket_id=4716, status="completed",
        created_at=datetime(2026, 1, 4, tzinfo=timezone.utc),
        issues=[{"number": 1, "title": "Issue A",
                 "url": "https://github.com/acme/widgets/issues/1",
                 "state": "closed", "complete_date": "not-a-date"}],
    )

    resp = client.get("/api/v1/ticket-journeys?model_name=helpdesk.ticket&ticket_id=4716&odoo_instance_id=1")
    assert resp.status_code == 200
    events = resp.json()["events"]
    kinds = [e["kind"] for e in events]
    assert kinds == [
        "analysis_requested", "analysis_completed", "analysis_requested",
        "issues_created", "issue_closed", "ready",
    ]
    timestamps = [e["ts"] for e in events]
    non_null = [datetime.fromisoformat(t) for t in timestamps if t is not None]
    assert non_null == sorted(non_null)
    first_null = timestamps.index(None)
    assert all(t is None for t in timestamps[first_null:])


def test_journey_instance_scoping(client_and_db):
    client, db = client_and_db
    _add_analysis(db, odoo_instance_id=1, ticket_id=4717, field_name="field_one")
    _add_analysis(db, odoo_instance_id=2, ticket_id=4717, field_name="field_two")

    resp = client.get("/api/v1/ticket-journeys?model_name=helpdesk.ticket&ticket_id=4717&odoo_instance_id=1")
    assert resp.status_code == 200
    summaries = [e["summary"] for e in resp.json()["events"]]
    assert any("field_one" in s for s in summaries)
    assert not any("field_two" in s for s in summaries)


def test_journey_requires_master_key():
    engine = create_engine_from_url(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0",
        api_key="s3cret", require_api_key=True,
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    try:
        resp = client.get("/api/v1/ticket-journeys?model_name=helpdesk.ticket&ticket_id=1")
        assert resp.status_code == 401
        resp = client.get(
            "/api/v1/ticket-journeys?model_name=helpdesk.ticket&ticket_id=1",
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()
