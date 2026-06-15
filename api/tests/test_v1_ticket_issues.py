"""Tests for the create-issues endpoints (github-issues handoff, Contract 1)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_github_client, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers

# The handoff doc's example payload, verbatim — the contract test anchor.
CONTRACT_PAYLOAD = {
    "ticket_id": 123,
    "model_name": "helpdesk.ticket",
    "github_url": "https://github.com/org/repo",
    "name": "Login page broken",
    "description": "We need a login page.",
    "analysis_html": "<h2>Summary</h2>...",
    "priority": "1",
    "ticket_url": "https://odoo.example.com/web#id=123&model=helpdesk.ticket&view_type=form",
}


@dataclass
class FakeJob:
    id: str = "rq:job:fake-1"


@dataclass
class FakeQueue:
    enqueued: list[tuple] = field(default_factory=list)

    def enqueue(self, func_path, params, **kwargs):
        self.enqueued.append((func_path, params, kwargs))
        return FakeJob(id=f"rq:job:fake-{len(self.enqueued)}")


@dataclass
class FakeGitHub:
    """Stands in for the GitHubClient used by the accept-time reachability
    check. Succeeds by default; set `error` to simulate an unreachable repo
    (PermanentError → 422) or a GitHub blip (TransientError → accepted)."""

    installation_id: int = 99
    error: Exception | None = None
    calls: list = field(default_factory=list)

    def get_repo_installation_id(self, owner: str, repo: str) -> int:
        self.calls.append((owner, repo))
        if self.error is not None:
            raise self.error
        return self.installation_id


@pytest.fixture()
def client_db_queue():
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
    app.dependency_overrides[get_github_client] = lambda: FakeGitHub()
    queue = FakeQueue()
    prev_queue = getattr(app.state, "rq_queue", None)
    app.state.rq_queue = queue
    yield TestClient(app), db, queue
    app.state.rq_queue = prev_queue
    app.dependency_overrides.clear()


def test_contract_payload_accepted_with_request_id(client_db_queue):
    client, db, queue = client_db_queue

    r = client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD)

    assert r.status_code == 202
    data = r.json()
    assert isinstance(data["request_id"], int)
    assert data["request_id"] >= 1  # Odoo's reva_issue_request_id default is 0
    assert data["status"] == "pending"

    func_path, params, kwargs = queue.enqueued[0]
    assert func_path == "worker.ticket_issue_tasks.run_ticket_issues"
    assert params["run_id"] == data["request_id"]
    assert params["ticket_url"] == CONTRACT_PAYLOAD["ticket_url"]

    row = writers.get_ticket_issue_run(db, data["request_id"])
    assert row["status"] == "pending"
    assert row["job_id"] == data["job_id"]


def test_empty_analysis_html_accepted(client_db_queue):
    client, _, _ = client_db_queue
    payload = {**CONTRACT_PAYLOAD, "analysis_html": ""}
    assert client.post("/api/v1/create-issues", json=payload).status_code == 202


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://github.com/org/repo",
        "https://gitlab.com/org/repo",
        "https://github.com/org",
        "https://github.com/org/repo/tree/main",
        "not a url",
    ],
)
def test_invalid_github_url_is_422_and_not_enqueued(client_db_queue, bad_url):
    client, _, queue = client_db_queue
    payload = {**CONTRACT_PAYLOAD, "github_url": bad_url}
    r = client.post("/api/v1/create-issues", json=payload)
    assert r.status_code == 422
    assert queue.enqueued == []


def test_missing_contract_field_is_422(client_db_queue):
    client, _, queue = client_db_queue
    payload = {k: v for k, v in CONTRACT_PAYLOAD.items() if k != "ticket_url"}
    assert client.post("/api/v1/create-issues", json=payload).status_code == 422
    assert queue.enqueued == []


def test_pending_dedup_returns_same_request_id(client_db_queue):
    """A re-click while a run is still pending must return the SAME request_id —
    Odoo overwrites its stored id, so a new one would orphan the in-flight
    run's callback (409 stale request_id)."""
    client, _, queue = client_db_queue
    first = client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD).json()
    second = client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD).json()
    assert second["request_id"] == first["request_id"]
    assert len(queue.enqueued) == 1  # no second job


def test_get_run_404_and_hides_pii(client_db_queue):
    client, _, _ = client_db_queue
    assert client.get("/api/v1/create-issues/999").status_code == 404

    created = client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD).json()
    r = client.get(f"/api/v1/create-issues/{created['request_id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pending"
    assert data["github_url"] == CONTRACT_PAYLOAD["github_url"]
    # customer-authored text is not exposed (mirrors input_text on ticket-analysis)
    assert "description" not in data
    assert "analysis_html" not in data


def test_requeue_guards_and_resume(client_db_queue):
    client, db, queue = client_db_queue
    created = client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD).json()
    run_id = created["request_id"]

    # pending runs cannot be requeued
    assert client.post(f"/api/v1/create-issues/{run_id}/requeue").status_code == 409
    assert client.post("/api/v1/create-issues/999/requeue").status_code == 404

    plan = [{"title": "A", "body": "b", "acceptance_criteria": [],
             "number": 42, "url": "https://github.com/org/repo/issues/42"}]
    from reva.db.models import TicketIssueRun
    with db.session() as s:
        row = s.get(TicketIssueRun, run_id)
        row.issues = plan
    writers.record_ticket_issue_run_failed(db, run_id, "boom")

    r = client.post(f"/api/v1/create-issues/{run_id}/requeue")
    assert r.status_code == 202
    assert r.json()["request_id"] == run_id
    assert len(queue.enqueued) == 2
    func_path, params, _ = queue.enqueued[1]
    assert func_path == "worker.ticket_issue_tasks.run_ticket_issues"
    assert params["run_id"] == run_id

    row = writers.get_ticket_issue_run(db, run_id)
    assert row["status"] == "pending"
    assert row["issues"] == plan  # plan survives so the rerun resumes


def test_requeue_409_when_text_purged_and_no_plan(client_db_queue):
    """Without a persisted plan, a requeue would re-plan from the purge
    sentinel and create garbage issues on GitHub — refuse it."""
    client, db, _ = client_db_queue
    created = client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD).json()
    run_id = created["request_id"]
    writers.record_ticket_issue_run_failed(db, run_id, "boom")

    from reva.db.models import TicketIssueRun
    with db.session() as s:
        row = s.get(TicketIssueRun, run_id)
        row.description = writers.PURGED_TICKET_TEXT
        row.analysis_html = writers.PURGED_TICKET_TEXT

    assert client.post(f"/api/v1/create-issues/{run_id}/requeue").status_code == 409


def test_enqueue_includes_contract_retry_policy(client_db_queue):
    """Contract 2 mandates retrying the Odoo callback on 5xx with 30/120/300s
    backoff — implemented as rq.Retry on the job, which resumes idempotently."""
    client, _, queue = client_db_queue
    client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD)
    _, _, kwargs = queue.enqueued[0]
    assert kwargs["retry"] is not None
    assert kwargs["retry"].max == 3


def test_enqueue_failure_marks_run_failed_and_returns_503(client_db_queue):
    """A queue outage must not leave a pending row no worker will ever process
    — the dedup would pin every future click to it (dead request_id)."""
    client, db, queue = client_db_queue

    def boom(*a, **k):
        raise ConnectionError("redis down")

    queue.enqueue = boom
    r = client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD)
    assert r.status_code == 503

    # the row is failed (not pending), so the next click starts a fresh run
    from reva.db.models import TicketIssueRun
    from sqlalchemy import select
    with db.session() as s:
        row = s.execute(select(TicketIssueRun)).scalars().one()
        assert row.status == "failed"
        assert "enqueue failed" in row.error_message


def test_requeue_allowed_for_stale_pending(client_db_queue):
    """A pending run whose job died without running (SIGKILLed worker) must be
    recoverable via requeue, not require manual DB surgery."""
    from datetime import datetime, timedelta, timezone

    client, db, queue = client_db_queue
    created = client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD).json()
    run_id = created["request_id"]

    # fresh pending -> still protected
    assert client.post(f"/api/v1/create-issues/{run_id}/requeue").status_code == 409

    from reva.db.models import TicketIssueRun
    with db.session() as s:
        s.get(TicketIssueRun, run_id).created_at = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        )

    r = client.post(f"/api/v1/create-issues/{run_id}/requeue")
    assert r.status_code == 202
    assert r.json()["request_id"] == run_id
    assert len(queue.enqueued) == 2


def test_requeue_409_when_another_run_is_pending(client_db_queue):
    client, db, _ = client_db_queue
    first = client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD).json()
    writers.record_ticket_issue_run_failed(db, first["request_id"], "boom")
    # plan persisted so the purge guard doesn't trip
    from reva.db.models import TicketIssueRun
    with db.session() as s:
        s.get(TicketIssueRun, first["request_id"]).issues = [
            {"title": "A", "number": 1, "url": "https://github.com/org/repo/issues/1"}
        ]
    second = client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD).json()
    assert second["request_id"] != first["request_id"]

    r = client.post(f"/api/v1/create-issues/{first['request_id']}/requeue")
    assert r.status_code == 409
    assert str(second["request_id"]) in r.json()["detail"]


def test_list_ticket_issue_runs_strips_plan_bodies(client_db_queue):
    """The runs feed (TUI) gets {number, title, url} refs only — plan bodies
    carry customer-derived text and must not leave via the list endpoint."""
    client, db, _ = client_db_queue
    created = client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD).json()
    run_id = created["request_id"]

    from reva.db.models import TicketIssueRun
    with db.session() as s:
        s.get(TicketIssueRun, run_id).issues = [
            {"title": "Implement login form", "body": "secret customer text",
             "acceptance_criteria": ["c1"], "number": 42,
             "url": "https://github.com/org/repo/issues/42", "state": "closed"},
            {"title": "Add session handling", "body": "more customer text",
             "acceptance_criteria": [], "number": None, "url": None},
        ]
    writers.record_ticket_issue_run_completed(
        db, run_id, writers.get_ticket_issue_run(db, run_id)["issues"]
    )

    r = client.get("/api/v1/ticket-issue-runs")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["id"] == run_id
    assert item["ticket_id"] == CONTRACT_PAYLOAD["ticket_id"]
    assert item["status"] == "completed"
    assert item["issues"] == [
        {"number": 42, "title": "Implement login form",
         "url": "https://github.com/org/repo/issues/42", "state": "closed"},
        {"number": None, "title": "Add session handling", "url": None, "state": None},
    ]
    assert "body" not in r.text
    assert "description" not in item


def test_list_ticket_issue_runs_status_filter_and_order(client_db_queue):
    client, db, _ = client_db_queue
    first = client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD).json()
    writers.record_ticket_issue_run_failed(db, first["request_id"], "boom")
    payload2 = {**CONTRACT_PAYLOAD, "ticket_id": 456}
    second = client.post("/api/v1/create-issues", json=payload2).json()

    data = client.get("/api/v1/ticket-issue-runs").json()
    assert [i["id"] for i in data["items"]] == [second["request_id"], first["request_id"]]

    failed = client.get("/api/v1/ticket-issue-runs?status=failed").json()
    assert failed["total"] == 1
    assert failed["items"][0]["id"] == first["request_id"]
    assert failed["items"][0]["error_message"] == "boom"
    assert failed["items"][0]["issues"] == []


def _docx_payload() -> dict:
    import base64
    import io
    import zipfile

    xml = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Spec text</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return {
        **CONTRACT_PAYLOAD,
        "model_name": "project.task",
        "description_docx": {
            "filename": "spec.docx",
            "content_base64": base64.b64encode(buf.getvalue()).decode(),
        },
    }


def test_docx_passed_to_job_but_not_stored(client_db_queue):
    """The document rides the RQ job params (Redis) at plan time, but is NOT
    persisted server-side — only a small basis digest is."""
    client, db, queue = client_db_queue
    payload = _docx_payload()

    r = client.post("/api/v1/create-issues", json=payload)

    assert r.status_code == 202
    _, params, _ = queue.enqueued[0]
    assert params["description_docx"]["filename"] == "spec.docx"
    row = writers.get_ticket_issue_run(db, r.json()["request_id"])
    assert "description_docx" not in row  # the doc is not kept on the server
    assert row["planning_basis"].startswith("docx:")


def test_docx_null_accepted(client_db_queue):
    client, _, _ = client_db_queue
    payload = {**CONTRACT_PAYLOAD, "description_docx": None}
    assert client.post("/api/v1/create-issues", json=payload).status_code == 202


def test_docx_invalid_base64_is_422(client_db_queue):
    client, _, queue = client_db_queue
    payload = _docx_payload()
    payload["description_docx"]["content_base64"] = "%%%not-base64%%%"
    r = client.post("/api/v1/create-issues", json=payload)
    assert r.status_code == 422
    assert "description_docx" in r.json()["detail"]
    assert queue.enqueued == []


def test_docx_non_zip_is_422(client_db_queue):
    import base64

    client, _, queue = client_db_queue
    payload = _docx_payload()
    payload["description_docx"]["content_base64"] = base64.b64encode(b"plain").decode()
    assert client.post("/api/v1/create-issues", json=payload).status_code == 422
    assert queue.enqueued == []


def test_docx_run_requeue_resumes_plan_without_the_doc(client_db_queue):
    client, db, queue = client_db_queue
    created = client.post("/api/v1/create-issues", json=_docx_payload()).json()
    run_id = created["request_id"]

    # not exposed on the ops endpoint (customer content + size)
    data = client.get(f"/api/v1/create-issues/{run_id}").json()
    assert "description_docx" not in data

    # a docx run that PRODUCED a plan resumes from it (no doc needed)
    writers.record_ticket_issue_run_failed(db, run_id, "boom")
    from reva.db.models import TicketIssueRun
    with db.session() as s:
        s.get(TicketIssueRun, run_id).issues = [
            {"title": "A", "number": 1, "url": "https://github.com/org/repo/issues/1"}
        ]
    r = client.post(f"/api/v1/create-issues/{run_id}/requeue")
    assert r.status_code == 202
    _, params, _ = queue.enqueued[-1]
    assert params["description_docx"] is None  # doc not retained; resume from plan


def test_docx_run_requeue_without_plan_is_409(client_db_queue):
    """A docx run that never planned can't be re-planned (doc is gone) — refuse
    rather than silently re-plan from the empty description."""
    client, db, _ = client_db_queue
    created = client.post("/api/v1/create-issues", json=_docx_payload()).json()
    run_id = created["request_id"]
    writers.record_ticket_issue_run_failed(db, run_id, "boom")  # no plan persisted

    r = client.post(f"/api/v1/create-issues/{run_id}/requeue")
    assert r.status_code == 409
    assert "re-trigger from Odoo" in r.json()["detail"]


# --- generalized attachment intake (.docx already covered above; + .pdf/.txt) -


def test_pdf_attachment_accepted(client_db_queue):
    """description_docx may now carry a .pdf (accept-time only sniffs the
    %PDF- magic; the worker does the full extraction)."""
    import base64

    client, _, queue = client_db_queue
    payload = {
        **CONTRACT_PAYLOAD, "model_name": "project.task",
        "description_docx": {
            "filename": "spec.pdf",
            "content_base64": base64.b64encode(b"%PDF-1.4\nminimal").decode(),
        },
    }
    r = client.post("/api/v1/create-issues", json=payload)
    assert r.status_code == 202
    _, params, _ = queue.enqueued[0]
    assert params["description_docx"]["filename"] == "spec.pdf"


def test_txt_attachment_accepted(client_db_queue):
    import base64

    client, _, _ = client_db_queue
    payload = {
        **CONTRACT_PAYLOAD, "model_name": "project.task",
        "description_docx": {
            "filename": "spec.txt",
            "content_base64": base64.b64encode(b"plain text spec").decode(),
        },
    }
    assert client.post("/api/v1/create-issues", json=payload).status_code == 202


def test_unsupported_attachment_type_is_422(client_db_queue):
    import base64

    client, _, queue = client_db_queue
    payload = {
        **CONTRACT_PAYLOAD,
        "description_docx": {
            "filename": "sheet.xlsx",
            "content_base64": base64.b64encode(b"PK\x03\x04ziplike").decode(),
        },
    }
    r = client.post("/api/v1/create-issues", json=payload)
    assert r.status_code == 422
    assert "description_docx" in r.json()["detail"]
    assert queue.enqueued == []


def test_unreachable_repo_is_422_and_not_enqueued(client_db_queue):
    """github_url parses but our App can't access the repo (GitHub 404 →
    PermanentError) — reject at accept time so Odoo shows it and rolls back."""
    from reva.errors import PermanentError

    client, _, queue = client_db_queue
    app.dependency_overrides[get_github_client] = lambda: FakeGitHub(
        error=PermanentError("GitHub 404 (installation)")
    )
    r = client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD)
    assert r.status_code == 422
    assert queue.enqueued == []


def test_transient_github_error_still_accepts(client_db_queue):
    """A GitHub blip at accept time must not become a user-facing rejection —
    accept and let the worker's own reachability check be the backstop."""
    from reva.errors import TransientError

    client, _, queue = client_db_queue
    app.dependency_overrides[get_github_client] = lambda: FakeGitHub(
        error=TransientError("GitHub 503")
    )
    r = client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD)
    assert r.status_code == 202
    assert len(queue.enqueued) == 1
