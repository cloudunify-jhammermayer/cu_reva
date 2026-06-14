"""Tests for POST /webhooks/github and GET /health.

Uses FastAPI TestClient + SQLite in-memory. No live network calls.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import GithubEvent, PendingReview, PullRequest, Repository, ReviewFinding
from reva.types import Finding, JobParams, ReviewResult


# --- helpers ------------------------------------------------------------------


_SECRET = "test_secret"
_DELIVERY = "delivery-abc-123"


def _sig(body: bytes, secret: str = _SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(client: TestClient, payload: dict, *, event: str = "pull_request",
          delivery: str = _DELIVERY, secret: str = _SECRET) -> ...:
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": _sig(body, secret),
            "X-GitHub-Event": event,
        },
    )


def _pr_payload(action: str = "opened", draft: bool = False, sha: str = "deadbeef") -> dict:
    return {
        "action": action,
        "installation": {"id": 99},
        "repository": {
            "id": 1001,
            "name": "widgets",
            "full_name": "acme/widgets",
            "default_branch": "main",
            "owner": {"login": "acme"},
        },
        "pull_request": {
            "id": 5001,
            "number": 42,
            "title": "Add feature",
            "state": "open",
            "draft": draft,
            "head": {"sha": sha, "ref": "feat/foo"},
            "base": {"ref": "main"},
            "user": {"login": "alice"},
        },
        "sender": {"login": "alice"},
    }


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
        github_app_id=12345,
        github_webhook_secret=_SECRET,
        github_private_key="fake",
        redis_url="redis://localhost:6379/0",
        debounce_seconds=600,
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app), db
    app.dependency_overrides.clear()


# --- webhook tests ------------------------------------------------------------


def test_valid_pr_opened_returns_202(client_and_db):
    client, _ = client_and_db
    resp = _post(client, _pr_payload("opened"))
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}


def test_pr_opened_creates_repo_pr_and_pending_review(client_and_db):
    client, db = client_and_db
    _post(client, _pr_payload("opened", sha="aabbccdd"))

    with db.session() as s:
        assert s.query(Repository).count() == 1
        assert s.query(PullRequest).count() == 1
        pending = s.query(PendingReview).one()
        assert pending.head_sha == "aabbccdd"
        assert pending.consumed is False
        assert pending.trigger_event == "opened"
        assert pending.review_mode == "diff"


def test_pr_synchronize_resets_debounce(client_and_db):
    client, db = client_and_db
    _post(client, _pr_payload("opened", sha="sha1"), delivery="d1")
    _post(client, _pr_payload("synchronize", sha="sha2"), delivery="d2")

    with db.session() as s:
        # Still only one pending_review row (debounce upsert)
        assert s.query(PendingReview).count() == 1
        pending = s.query(PendingReview).one()
        assert pending.head_sha == "sha2"
        assert pending.trigger_event == "synchronize"


def test_pr_closed_action_does_not_create_pending_review(client_and_db):
    client, db = client_and_db
    _post(client, _pr_payload("closed"))

    with db.session() as s:
        assert s.query(PendingReview).count() == 0


def _seed_posted_finding(db) -> int:
    """Seed repo + PR (matching _pr_payload) + a completed review with one
    posted finding. Returns the finding id."""
    repo_id = writers.upsert_repository(
        db, github_repository_id=1001, owner="acme", name="widgets",
        default_branch="main", installation_id=99,
    )
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=5001, pr_number=42,
        title="Add feature", author_login="alice", base_branch="main",
        head_branch="feat/foo", head_sha="deadbeef", state="open", draft=False,
    )
    params = JobParams(
        repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
        installation_id=99, review_mode="diff", trigger_event="opened",
    )
    result = ReviewResult(
        status="completed", summary="s", risk_level="high",
        findings=[Finding(severity="major", category="bug", file="x.py", line_start=1,
                          line_end=1, title="t", body="b", confidence=0.8, is_odoo_specific=False)],
        model="claude-sonnet-4-6", started_at=None, completed_at=None,
    )
    writers.record_review_completed(db, params, result)
    with db.session() as s:
        fid = s.query(ReviewFinding).one().id
    writers.attach_finding_comment_ids(db, {fid: 777})  # mark posted
    return fid


def test_pr_closed_merged_marks_open_findings(client_and_db):
    client, db = client_and_db
    fid = _seed_posted_finding(db)
    payload = _pr_payload("closed")
    payload["pull_request"]["merged"] = True
    _post(client, payload)

    with db.session() as s:
        assert s.get(ReviewFinding, fid).outcome == "still_open_at_merge"


def test_pr_closed_unmerged_marks_nothing(client_and_db):
    client, db = client_and_db
    fid = _seed_posted_finding(db)
    payload = _pr_payload("closed")
    payload["pull_request"]["merged"] = False
    _post(client, payload)

    with db.session() as s:
        assert s.get(ReviewFinding, fid).outcome == "open"


def test_draft_pr_does_not_create_pending_review(client_and_db):
    client, db = client_and_db
    _post(client, _pr_payload("opened", draft=True))

    with db.session() as s:
        assert s.query(PendingReview).count() == 0


def test_ready_for_review_creates_pending_review(client_and_db):
    client, db = client_and_db
    # Draft PR opened — skipped
    _post(client, _pr_payload("opened", draft=True), delivery="d1")
    # Same PR transitions to ready — should be scheduled
    payload = _pr_payload("ready_for_review", draft=False)
    payload["pull_request"]["draft"] = False
    _post(client, payload, delivery="d2")

    with db.session() as s:
        assert s.query(PendingReview).count() == 1
        assert s.query(PendingReview).one().trigger_event == "ready_for_review"


def test_invalid_signature_returns_401(client_and_db):
    client, _ = client_and_db
    payload = _pr_payload()
    body = json.dumps(payload).encode()
    resp = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Delivery": _DELIVERY,
            "X-Hub-Signature-256": "sha256=badhash",
            "X-GitHub-Event": "pull_request",
        },
    )
    assert resp.status_code == 401


def test_duplicate_delivery_returns_duplicate(client_and_db):
    client, db = client_and_db
    _post(client, _pr_payload("opened"), delivery="dup-1")
    resp = _post(client, _pr_payload("synchronize", sha="newsha"), delivery="dup-1")

    assert resp.status_code == 202
    assert resp.json() == {"status": "duplicate"}

    with db.session() as s:
        # Only one github_event row (the first delivery)
        assert s.query(GithubEvent).count() == 1
        # Only one pending_review (the first push's sha)
        pending = s.query(PendingReview).one()
        assert pending.head_sha == "deadbeef"


def test_event_stored_in_github_events(client_and_db):
    client, db = client_and_db
    _post(client, _pr_payload("opened"))

    with db.session() as s:
        ev = s.query(GithubEvent).one()
        assert ev.delivery_id == _DELIVERY
        assert ev.event_type == "pull_request"
        assert ev.action == "opened"
        assert ev.repository_full_name == "acme/widgets"
        assert ev.sender_login == "alice"


def test_successful_delivery_is_marked_processed(client_and_db):
    client, db = client_and_db
    _post(client, _pr_payload("opened"))
    with db.session() as s:
        assert s.query(GithubEvent).one().processed is True


def test_downstream_failure_leaves_delivery_reprocessable(client_and_db, monkeypatch):
    """A DB failure after the event is recorded must NOT mark it processed, so a
    GitHub redelivery reprocesses it instead of silently dropping the review."""
    client, db = client_and_db
    from reva.db import writers as w

    real = w.upsert_pending_review
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient db blip")
        return real(*args, **kwargs)

    monkeypatch.setattr(w, "upsert_pending_review", flaky)

    # First delivery: downstream write fails. Event recorded but not processed.
    with pytest.raises(RuntimeError):
        _post(client, _pr_payload("opened", sha="aabb"), delivery="retry-me")
    with db.session() as s:
        assert s.query(GithubEvent).one().processed is False
        assert s.query(PendingReview).count() == 0

    # GitHub redelivers the same id: now it completes.
    resp = _post(client, _pr_payload("opened", sha="aabb"), delivery="retry-me")
    assert resp.json() == {"status": "accepted"}
    with db.session() as s:
        assert s.query(PendingReview).count() == 1
        assert s.query(GithubEvent).one().processed is True


def test_unknown_event_type_accepted_and_stored(client_and_db):
    client, db = client_and_db
    resp = _post(client, {"action": "labeled"}, event="issues")

    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}
    with db.session() as s:
        ev = s.query(GithubEvent).one()
        assert ev.event_type == "issues"
        assert s.query(PendingReview).count() == 0


def test_missing_signature_header_returns_422(client_and_db):
    client, _ = client_and_db
    body = json.dumps(_pr_payload()).encode()
    resp = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Delivery": _DELIVERY,
            "X-GitHub-Event": "pull_request",
            # X-Hub-Signature-256 intentionally omitted
        },
    )
    assert resp.status_code == 422


# --- comment trigger authorization --------------------------------------------


def _comment_payload(body: str, *, association: str = "OWNER",
                     sender_type: str = "User") -> dict:
    return {
        "action": "created",
        "installation": {"id": 99},
        "repository": {
            "id": 1001,
            "name": "widgets",
            "full_name": "acme/widgets",
            "default_branch": "main",
            "owner": {"login": "acme"},
        },
        "issue": {"number": 42, "pull_request": {"url": "https://api/pr/42"}},
        "comment": {"body": body, "author_association": association},
        "sender": {"login": "bob", "type": sender_type},
    }


def _seed_pr(client) -> None:
    _post(client, _pr_payload("opened"), delivery="seed")


def test_comment_review_by_owner_creates_pending(client_and_db):
    client, db = client_and_db
    _seed_pr(client)
    resp = _post(client, _comment_payload("/review"), event="issue_comment",
                 delivery="c1")
    assert resp.status_code == 202
    with db.session() as s:
        pending = s.query(PendingReview).all()
        assert any(p.trigger_event == "comment" and p.review_mode == "diff"
                   for p in pending)


def test_comment_review_on_unseen_pr_fetches_and_queues(client_and_db):
    # PR predates the installation, so REVA has no row for it. A /review comment
    # must fetch the PR from GitHub, record it, and still queue the review.
    client, db = client_and_db
    fetched_pr = {
        "id": 5001,
        "number": 42,
        "title": "Pre-existing PR",
        "state": "open",
        "draft": False,
        "head": {"sha": "fetchsha", "ref": "feat/foo"},
        "base": {"ref": "main"},
        "user": {"login": "alice"},
    }
    fake = _FakeGitHub(pr=fetched_pr)
    app.state.github = fake
    try:
        resp = _post(client, _comment_payload("/review"), event="issue_comment",
                     delivery="unseen1")
    finally:
        app.state.github = None
    assert resp.status_code == 202
    assert fake.fetched == [{"owner": "acme", "repo": "widgets", "pr": 42}]
    with db.session() as s:
        assert s.query(PullRequest).count() == 1
        pending = s.query(PendingReview).all()
        assert any(p.trigger_event == "comment" and p.head_sha == "fetchsha"
                   for p in pending)


def test_comment_review_on_unseen_pr_fetch_failure_logs_not_found(client_and_db):
    client, db = client_and_db

    class Boom(_FakeGitHub):
        def get_pull_request(self, token, owner, repo, pr_number):
            raise RuntimeError("github down")

    app.state.github = Boom()
    try:
        resp = _post(client, _comment_payload("/review"), event="issue_comment",
                     delivery="unseen2")
    finally:
        app.state.github = None
    assert resp.status_code == 202  # webhook still succeeds
    with db.session() as s:
        assert s.query(PendingReview).count() == 0


def test_comment_review_by_outsider_is_ignored(client_and_db):
    client, db = client_and_db
    _seed_pr(client)
    _post(client, _comment_payload("/review", association="NONE"),
          event="issue_comment", delivery="c1")
    with db.session() as s:
        # Only the debounced pending from the seed PR — no comment trigger.
        assert all(p.trigger_event != "comment"
                   for p in s.query(PendingReview).all())


class _FakeQueue:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, func_name, *args, **kwargs):
        self.enqueued.append({"func": func_name, "args": args})
        return type("Job", (), {"id": "job-1"})()


def _review_comment_payload(association: str = "MEMBER", in_reply_to: int = 555) -> dict:
    return {
        "action": "created",
        "installation": {"id": 99},
        "repository": {"name": "widgets", "owner": {"login": "acme"}},
        "pull_request": {"number": 42},
        "comment": {
            "in_reply_to_id": in_reply_to,
            "author_association": association,
            "body": "Why is this a problem?",
        },
        "sender": {"login": "alice", "type": "User"},
    }


def test_oversized_body_is_rejected_413(client_and_db, monkeypatch):
    """SECU-12: a body over the app-level cap is rejected with 413 before the
    handler reads it into memory (defense-in-depth beside nginx)."""
    import app.main as main
    monkeypatch.setattr(main, "_MAX_BODY_BYTES", 10)
    client, _ = client_and_db
    resp = client.post("/webhooks/github", content=b"x" * 50,
                       headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "d",
                                "X-Hub-Signature-256": "sha256=x"})
    assert resp.status_code == 413


def test_malformed_pr_payload_is_accepted_not_500(client_and_db):
    """CORR-13: a partial/malformed payload must not 500 (which loops GitHub's
    redelivery). The event is marked processed and accepted with a warning."""
    client, db = client_and_db
    bad = _pr_payload("opened")
    del bad["pull_request"]["head"]  # drop a hard-subscripted key
    resp = _post(client, bad, delivery="malformed1")
    assert resp.status_code == 202
    from reva.db.models import GithubEvent
    with db.session() as s:
        ev = s.query(GithubEvent).one()
        assert ev.processed_at is not None  # marked processed → no redelivery loop


def test_review_comment_reply_by_member_enqueues(client_and_db):
    """SECU-3: a trusted member replying to a REVA inline comment triggers a reply."""
    client, _ = client_and_db
    q = _FakeQueue()
    app.state.rq_queue = q
    try:
        resp = _post(client, _review_comment_payload(association="MEMBER"),
                     event="pull_request_review_comment", delivery="rc1")
    finally:
        app.state.rq_queue = None
    assert resp.status_code == 202
    assert [e["func"] for e in q.enqueued] == ["worker.runner.run_comment_reply"]


def test_review_comment_reply_by_outsider_is_ignored(client_and_db):
    """SECU-3: an untrusted commenter (e.g. external PR author) must NOT be able
    to trigger a paid reply by replying to REVA's inline comment."""
    client, _ = client_and_db
    q = _FakeQueue()
    app.state.rq_queue = q
    try:
        resp = _post(client, _review_comment_payload(association="NONE"),
                     event="pull_request_review_comment", delivery="rc2")
    finally:
        app.state.rq_queue = None
    assert resp.status_code == 202  # event accepted/stored, but no reply enqueued
    assert q.enqueued == []


class _FakeGitHub:
    def __init__(self, pr=None):
        self.comments = []
        self._pr = pr
        self.fetched = []
        self.fetched_repos = []

    def get_installation_token(self, installation_id):
        return "tok"

    def create_issue_comment(self, token, owner, repo, pr_number, body):
        self.comments.append({"owner": owner, "repo": repo, "pr": pr_number, "body": body})
        return 1

    def get_pull_request(self, token, owner, repo, pr_number):
        self.fetched.append({"owner": owner, "repo": repo, "pr": pr_number})
        if self._pr is None:
            raise AssertionError("get_pull_request called without a configured PR")
        return self._pr

    def get_repo(self, token, owner, repo):
        self.fetched_repos.append({"owner": owner, "repo": repo})
        return {
            "id": 7000 + len(self.fetched_repos),
            "name": repo,
            "full_name": f"{owner}/{repo}",
            "owner": {"login": owner},
            "default_branch": "main",
        }


def test_comment_trigger_posts_ack_comment(client_and_db):
    client, _ = client_and_db
    _seed_pr(client)
    fake = _FakeGitHub()
    app.state.github = fake
    try:
        _post(client, _comment_payload("/deep-review"), event="issue_comment", delivery="ack1")
    finally:
        app.state.github = None
    assert len(fake.comments) == 1
    c = fake.comments[0]
    assert c["pr"] == 42 and c["owner"] == "acme" and c["repo"] == "widgets"
    assert "REVA" in c["body"] and "deep review" in c["body"]


def test_comment_trigger_ack_failure_does_not_break_webhook(client_and_db):
    client, db = client_and_db
    _seed_pr(client)

    class Boom(_FakeGitHub):
        def get_installation_token(self, installation_id):
            raise RuntimeError("github down")

    app.state.github = Boom()
    try:
        resp = _post(client, _comment_payload("/review"), event="issue_comment", delivery="ack2")
    finally:
        app.state.github = None
    assert resp.status_code == 202  # webhook still succeeds
    with db.session() as s:
        # the review was still queued despite the ack failing
        assert any(p.trigger_event == "comment" for p in s.query(PendingReview).all())


def test_comment_review_by_bot_is_ignored(client_and_db):
    client, db = client_and_db
    _seed_pr(client)
    _post(client, _comment_payload("/review", sender_type="Bot"),
          event="issue_comment", delivery="c1")
    with db.session() as s:
        assert all(p.trigger_event != "comment"
                   for p in s.query(PendingReview).all())


# --- auto-review (PR event) ack ----------------------------------------------


def _post_pr_with_github(client, action, *, draft=False, delivery="prack"):
    fake = _FakeGitHub()
    app.state.github = fake
    try:
        payload = _pr_payload(action, draft=draft)
        if action == "ready_for_review":
            payload["pull_request"]["draft"] = False
        _post(client, payload, delivery=delivery)
    finally:
        app.state.github = None
    return fake


def test_pr_opened_posts_ack_comment(client_and_db):
    client, _ = client_and_db
    fake = _post_pr_with_github(client, "opened")
    assert len(fake.comments) == 1
    c = fake.comments[0]
    assert c["pr"] == 42 and c["owner"] == "acme" and c["repo"] == "widgets"
    assert "REVA" in c["body"] and "standard review" in c["body"]


def test_pr_reopened_posts_ack_comment(client_and_db):
    client, _ = client_and_db
    fake = _post_pr_with_github(client, "reopened")
    assert len(fake.comments) == 1


def test_pr_ready_for_review_posts_ack_comment(client_and_db):
    client, _ = client_and_db
    fake = _post_pr_with_github(client, "ready_for_review")
    assert len(fake.comments) == 1


def test_pr_synchronize_does_not_post_ack(client_and_db):
    client, _ = client_and_db
    fake = _post_pr_with_github(client, "synchronize")
    assert fake.comments == []


def test_draft_pr_opened_does_not_post_ack(client_and_db):
    client, _ = client_and_db
    fake = _post_pr_with_github(client, "opened", draft=True)
    assert fake.comments == []


# --- health -------------------------------------------------------------------


class _FakeRedis:
    def __init__(self, ok=True):
        self._ok = ok

    def ping(self):
        if not self._ok:
            raise RuntimeError("redis down")
        return True


def test_health_returns_ok(client_and_db):
    client, _ = client_and_db
    from app.dependencies import get_redis
    app.dependency_overrides[get_redis] = lambda: _FakeRedis(ok=True)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": True, "redis": True}


def test_health_degraded_when_redis_down(client_and_db):
    client, _ = client_and_db
    from app.dependencies import get_redis
    app.dependency_overrides[get_redis] = lambda: _FakeRedis(ok=False)
    resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] is True
    assert body["redis"] is False


def test_review_all_command_queues_diff_all_mode(client_and_db):
    client, db = client_and_db
    _seed_pr(client)
    resp = _post(client, _comment_payload("/review-all"), event="issue_comment",
                 delivery="reviewall")
    assert resp.status_code == 202
    with db.session() as s:
        pending = s.query(PendingReview).all()
        assert any(p.trigger_event == "comment" and p.review_mode == "diff-all"
                   for p in pending)


def test_draft_pr_logs_ignored_reason(client_and_db):
    """Observability: a skipped draft PR logs why (was a silent return)."""
    import structlog
    client, _ = client_and_db
    with structlog.testing.capture_logs() as logs:
        _post(client, _pr_payload("opened", draft=True), delivery="draftlog")
    ignored = [e for e in logs if e.get("event") == "pr_event_ignored"]
    assert ignored and ignored[0]["reason"] == "draft PR"


# --- installation → auto-audit ------------------------------------------------


def _installation_payload(action: str = "created", repos=None) -> dict:
    return {
        "action": action,
        "installation": {"id": 99, "account": {"login": "acme"}},
        "repositories": repos
        if repos is not None
        else [
            {"id": 1001, "name": "widgets", "full_name": "acme/widgets"},
            {"id": 1002, "name": "gadgets", "full_name": "acme/gadgets"},
        ],
        "sender": {"login": "alice", "type": "User"},
    }


def _installation_repos_payload(action: str = "added", added=None) -> dict:
    return {
        "action": action,
        "installation": {"id": 99, "account": {"login": "acme"}},
        "repositories_added": added
        if added is not None
        else [{"id": 1003, "name": "sprockets", "full_name": "acme/sprockets"}],
        "repositories_removed": [],
        "sender": {"login": "alice", "type": "User"},
    }


def test_installation_created_audits_every_repo(client_and_db):
    client, db = client_and_db
    q = _FakeQueue()
    fake = _FakeGitHub()
    app.state.rq_queue = q
    app.state.github = fake
    try:
        resp = _post(client, _installation_payload(), event="installation",
                     delivery="inst1")
    finally:
        app.state.rq_queue = None
        app.state.github = None

    assert resp.status_code == 202
    # One repo registered + one audit enqueued per granted repo.
    with db.session() as s:
        assert s.query(Repository).count() == 2
    assert [e["func"] for e in q.enqueued] == [
        "worker.audit_tasks.run_audit",
        "worker.audit_tasks.run_audit",
    ]
    params = q.enqueued[0]["args"][0]
    assert params["installation_id"] == 99
    assert params["requested_by"] == "installation"
    assert isinstance(params["repository_id"], int)


def test_installation_non_created_action_does_nothing(client_and_db):
    client, db = client_and_db
    q = _FakeQueue()
    app.state.rq_queue = q
    app.state.github = _FakeGitHub()
    try:
        resp = _post(client, _installation_payload(action="deleted"),
                     event="installation", delivery="instdel")
    finally:
        app.state.rq_queue = None
        app.state.github = None
    assert resp.status_code == 202
    with db.session() as s:
        assert s.query(Repository).count() == 0
    assert q.enqueued == []


def test_installation_repositories_added_audits_new_repos(client_and_db):
    client, db = client_and_db
    q = _FakeQueue()
    app.state.rq_queue = q
    app.state.github = _FakeGitHub()
    try:
        resp = _post(client, _installation_repos_payload(),
                     event="installation_repositories", delivery="instrepo1")
    finally:
        app.state.rq_queue = None
        app.state.github = None
    assert resp.status_code == 202
    with db.session() as s:
        assert s.query(Repository).count() == 1
    assert [e["func"] for e in q.enqueued] == ["worker.audit_tasks.run_audit"]


def test_installation_repo_fetch_failure_skips_repo_not_delivery(client_and_db):
    client, db = client_and_db

    class Boom(_FakeGitHub):
        def get_repo(self, token, owner, repo):
            if repo == "widgets":
                raise RuntimeError("github down")
            return super().get_repo(token, owner, repo)

    q = _FakeQueue()
    app.state.rq_queue = q
    app.state.github = Boom()
    try:
        resp = _post(client, _installation_payload(), event="installation",
                     delivery="instfail")
    finally:
        app.state.rq_queue = None
        app.state.github = None
    # Delivery still accepted; only the healthy repo is registered + audited.
    assert resp.status_code == 202
    with db.session() as s:
        assert s.query(Repository).count() == 1
    assert [e["func"] for e in q.enqueued] == ["worker.audit_tasks.run_audit"]


# --- issues events (per-issue state sync) ---------------------------------------


def _issues_payload(action: str = "closed", labels: list[str] | None = None,
                    number: int = 42) -> dict:
    return {
        "action": action,
        "installation": {"id": 99},
        "repository": {"id": 1001, "name": "widgets", "full_name": "acme/widgets",
                       "owner": {"login": "acme"}},
        "issue": {
            "number": number,
            "title": "Implement login form",
            "state": "closed" if action == "closed" else "open",
            "labels": [{"name": name} for name in (labels if labels is not None else ["reva-ticket"])],
        },
        "sender": {"login": "alice", "type": "User"},
    }


def test_issue_closed_with_ticket_label_enqueues_state_sync(client_and_db):
    client, _ = client_and_db
    q = _FakeQueue()
    app.state.rq_queue = q
    try:
        r = _post(client, _issues_payload("closed"), event="issues", delivery="d-iss-1")
        assert r.status_code == 202
        assert len(q.enqueued) == 1
        assert q.enqueued[0]["func"] == "worker.ticket_issue_tasks.sync_ticket_issue_state"
        assert q.enqueued[0]["args"][0] == {
            "owner": "acme", "repo": "widgets", "number": 42, "state": "closed",
        }
    finally:
        app.state.rq_queue = None


def test_issue_reopened_enqueues_open_state(client_and_db):
    client, _ = client_and_db
    q = _FakeQueue()
    app.state.rq_queue = q
    try:
        _post(client, _issues_payload("reopened"), event="issues", delivery="d-iss-2")
        assert q.enqueued[0]["args"][0]["state"] == "open"
    finally:
        app.state.rq_queue = None


def test_issue_without_ticket_label_is_ignored(client_and_db):
    client, _ = client_and_db
    q = _FakeQueue()
    app.state.rq_queue = q
    try:
        r = _post(client, _issues_payload("closed", labels=["bug"]),
                  event="issues", delivery="d-iss-3")
        assert r.status_code == 202  # stored, but no job
        assert q.enqueued == []
    finally:
        app.state.rq_queue = None


def test_issue_irrelevant_action_is_ignored(client_and_db):
    client, _ = client_and_db
    q = _FakeQueue()
    app.state.rq_queue = q
    try:
        _post(client, _issues_payload("labeled"), event="issues", delivery="d-iss-4")
        assert q.enqueued == []
    finally:
        app.state.rq_queue = None
