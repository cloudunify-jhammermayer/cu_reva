"""Tests for run_audit: spend recording + budget pre-check (SECU-4/CORR-11)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import AuditRun, ClaudeSpend
from reva.types import AuditResult
from worker.audit_tasks import run_audit
from worker.runner import WorkerContext, set_context


class FakeAuditor:
    def __init__(self, result: AuditResult):
        self.result = result
        self.called = False

    def execute(self, params):
        self.called = True
        return self.result


def _ctx(db, auditor, budget=None, github=None) -> WorkerContext:
    return WorkerContext(
        db=db, claude=None, runner=None, github=github,  # type: ignore[arg-type]
        reviewer=None, auditor=auditor, ticket_analyzer=None,  # type: ignore[arg-type]
        verifier=None, daily_budget_usd=budget,  # type: ignore[arg-type]
    )


class FakeGitHub:
    def __init__(self, existing_markers=()):
        self.existing_markers = set(existing_markers)
        self.created: list[dict] = []
        self.labels_ensured: list[str] = []
        self._next = 100

    def get_installation_token(self, installation_id):
        return "ghs_token"

    def ensure_label(self, token, owner, repo, name, color="5319e7", description=""):
        self.labels_ensured.append(name)

    def issue_exists_with_marker(self, token, owner, repo, marker):
        return marker in self.existing_markers

    def create_issue(self, token, owner, repo, title, body, labels=None):
        self.created.append({"title": title, "body": body, "owner": owner,
                             "repo": repo, "labels": labels})
        n = self._next
        self._next += 1
        return {"number": n, "url": f"https://github.com/{owner}/{repo}/issues/{n}"}


def _result_with(findings, cost=1.0) -> AuditResult:
    return AuditResult(status="completed", summary="ok", findings=findings,
                       model="claude-opus-4-8", estimated_cost_usd=cost)


@pytest.fixture()
def db():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    d = Database(engine)
    repo_id = writers.upsert_repository(
        d, github_repository_id=1, owner="acme", name="widgets",
        default_branch="main", installation_id=500,
    )
    return d, repo_id


def _result(cost: float) -> AuditResult:
    return AuditResult(status="completed", summary="ok", findings=[],
                       model="claude-opus-4-8", estimated_cost_usd=cost)


def _since():
    return datetime.now(timezone.utc) - timedelta(days=1)


def _finding(severity="major", title="SQL injection", file="app/x.py"):
    from reva.types import Finding
    return Finding(
        severity=severity, category="security", file=file,
        line_start=10, line_end=12, title=title, body="details",
        suggestion="fix it", confidence=0.9,
    )


def test_audit_findings_persist_and_issue_number_roundtrip(db):
    d, repo_id = db
    from reva.db.models import AuditFinding, AuditRun

    with d.session() as s:
        run = AuditRun(repository_id=repo_id, status="completed")
        s.add(run)
        s.flush()
        audit_id = run.id
        s.commit()

    ids = writers.insert_audit_findings(
        d, audit_id, [_finding(severity="critical", title="RCE"), _finding(severity="minor", title="nit")]
    )
    assert len(ids) == 2

    writers.set_audit_finding_issue_number(d, ids[0], 4242)

    with d.session() as s:
        rows = s.query(AuditFinding).filter_by(audit_run_id=audit_id).order_by(AuditFinding.id).all()
        assert [r.severity for r in rows] == ["critical", "minor"]
        assert rows[0].github_issue_number == 4242
        assert rows[1].github_issue_number is None


def test_audit_issue_body_is_structured():
    from worker.audit_tasks import _format_audit_issue_body

    f = _finding(severity="major", title="SQLi", file="custom_addons/foo.py")
    body = _format_audit_issue_body(
        f, marker="revaaudit123", owner="acme", repo="widgets",
        branch="main", audit_id=7,
    )
    # Severity badge + category + confidence on the lead line.
    assert "🟠" in body and "Major" in body and "`security`" in body
    # Clickable location link to the file on the default branch, with line anchor.
    assert "[`custom_addons/foo.py:10`]" in body
    assert "https://github.com/acme/widgets/blob/main/custom_addons/foo.py#L10" in body
    # Clear sections + footer + hidden dedup marker.
    assert "### Description" in body
    assert "### Suggested fix" in body
    assert "run #7" in body
    assert "<!-- revaaudit123 -->" in body


def test_run_audit_persists_all_findings_and_issues_only_major_critical(db):
    d, repo_id = db
    findings = [
        _finding(severity="critical", title="RCE", file="a.py"),
        _finding(severity="major", title="SQLi", file="b.py"),
        _finding(severity="minor", title="nit", file="c.py"),
        _finding(severity="info", title="fyi", file="d.py"),
    ]
    gh = FakeGitHub()
    set_context(_ctx(d, FakeAuditor(_result_with(findings)), github=gh))

    out = run_audit({"repository_id": repo_id, "installation_id": 500})
    assert out["status"] == "completed"

    from reva.db.models import AuditFinding
    with d.session() as s:
        rows = s.query(AuditFinding).order_by(AuditFinding.id).all()
        assert len(rows) == 4  # ALL findings persisted
        issued = {r.severity for r in rows if r.github_issue_number is not None}
        assert issued == {"critical", "major"}  # only these became issues

    assert len(gh.created) == 2
    assert all(c["title"].startswith("[REVA audit]") for c in gh.created)
    assert gh.created[0]["repo"] == "widgets"
    assert gh.created[0]["labels"] == ["reva-audit"]
    assert "reva-audit" in gh.labels_ensured


def test_run_audit_skips_issue_when_open_issue_exists(db):
    d, repo_id = db
    from worker.audit_tasks import _audit_finding_marker

    f = _finding(severity="critical", title="RCE", file="a.py")
    marker = _audit_finding_marker("acme", "widgets", f)
    gh = FakeGitHub(existing_markers={marker})
    set_context(_ctx(d, FakeAuditor(_result_with([f])), github=gh))

    run_audit({"repository_id": repo_id, "installation_id": 500})

    assert gh.created == []  # deduped — no new issue
    from reva.db.models import AuditFinding
    with d.session() as s:
        assert s.query(AuditFinding).one().github_issue_number is None


def test_run_audit_records_spend_to_ledger(db):
    d, repo_id = db
    auditor = FakeAuditor(_result(cost=3.5))
    set_context(_ctx(d, auditor))

    out = run_audit({"repository_id": repo_id, "installation_id": 500})

    assert out["status"] == "completed"
    assert auditor.called
    assert writers.sum_estimated_cost_since(d, _since()) == pytest.approx(3.5)


def test_run_audit_declines_when_over_budget_without_running(db):
    """SECU-4: an audit is the most expensive path — it must respect the cap.
    A new audit is declined when over budget; no AuditRun row, auditor not run."""
    d, repo_id = db
    writers.record_claude_spend(d, "review", 50.0)  # already over the cap
    auditor = FakeAuditor(_result(cost=3.5))
    set_context(_ctx(d, auditor, budget=10.0))

    out = run_audit({"repository_id": repo_id, "installation_id": 500})

    assert out["status"] == "declined"
    assert auditor.called is False
    with d.session() as s:
        assert s.query(AuditRun).count() == 0
        # no new spend recorded for the declined audit
        assert s.query(ClaudeSpend).count() == 1


def test_run_audit_marks_row_failed_on_error(db):
    """CORR-12: audits aren't RQ-retried, so a failure must mark the row failed —
    not leave it stuck in 'started' forever."""
    from reva.errors import TransientError

    d, repo_id = db

    class _Boom:
        def execute(self, params):
            raise TransientError("network blip mid-audit")

    set_context(_ctx(d, _Boom()))
    with pytest.raises(TransientError):
        run_audit({"repository_id": repo_id, "installation_id": 500})

    with d.session() as s:
        row = s.query(AuditRun).one()  # exactly one row, and it's terminal
        assert row.status == "failed"
        assert "network blip" in (row.error_message or "")
