"""Tests for GET /api/v1/audit-findings."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers
from reva.types import Finding


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
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app), db
    app.dependency_overrides.clear()


def _seed(db, *, severity="critical", issue_number=None):
    from reva.db.models import AuditFinding, AuditRun
    repo_id = writers.upsert_repository(
        db, github_repository_id=1001, owner="acme", name="widgets",
        default_branch="main", installation_id=99,
    )
    with db.session() as s:
        run = AuditRun(repository_id=repo_id, status="completed")
        s.add(run)
        s.flush()
        audit_id = run.id
        s.commit()
    ids = writers.insert_audit_findings(db, audit_id, [
        Finding(severity=severity, category="security", file="a.py", line_start=5,
                title="RCE", body="details", confidence=0.9),
    ])
    if issue_number is not None:
        writers.set_audit_finding_issue_number(db, ids[0], issue_number)
    return audit_id


def test_list_audit_findings_returns_repo_and_fields(client_and_db):
    client, db = client_and_db
    _seed(db, issue_number=42)

    r = client.get("/api/v1/audit-findings")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["repo_full_name"] == "acme/widgets"
    assert item["severity"] == "critical"
    assert item["title"] == "RCE"
    assert item["file_path"] == "a.py"
    assert item["github_issue_number"] == 42


def test_filter_by_severity(client_and_db):
    client, db = client_and_db
    _seed(db, severity="minor")

    assert client.get("/api/v1/audit-findings?severity=critical").json()["total"] == 0
    assert client.get("/api/v1/audit-findings?severity=minor").json()["total"] == 1


def test_empty_when_no_audits(client_and_db):
    client, _ = client_and_db
    r = client.get("/api/v1/audit-findings")
    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 0}


def test_list_audit_runs(client_and_db):
    client, db = client_and_db
    aid = _seed(db, severity="critical", issue_number=42)

    data = client.get("/api/v1/audits").json()
    assert data["total"] == 1
    run = data["items"][0]
    assert run["id"] == aid
    assert run["repo_full_name"] == "acme/widgets"
    assert run["status"] == "completed"
    assert run["issued_count"] == 1  # one finding became an issue


def test_audit_findings_filter_by_run(client_and_db):
    client, db = client_and_db
    a1 = _seed(db, severity="critical")
    a2 = _seed(db, severity="minor")

    assert client.get("/api/v1/audit-findings").json()["total"] == 2
    only = client.get(f"/api/v1/audit-findings?audit_run_id={a1}").json()
    assert only["total"] == 1
    assert only["items"][0]["audit_run_id"] == a1
    assert a2 != a1
