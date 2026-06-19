"""Tests for ticket_issue_runner.run_ticket_issues.

Real SQLite DB so writer + resume paths are exercised against SQL.
Fakes for TicketIssuePlanner, GitHubClient and OdooCallbackClient.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.errors import PermanentError, TransientError
from reva.types import (
    ClaudeResponse,
    TicketIssueItem,
    TicketIssueJobParams,
    TicketIssuePlan,
)
from worker.runner import WorkerContext, set_context
from worker.ticket_issue_runner import run_ticket_issues


# --- Fakes -------------------------------------------------------------------


@dataclass
class FakePlanner:
    plan: TicketIssuePlan | None = None
    raise_exc: Exception | None = None
    call_count: int = 0

    def plan_with_response(
        self, params: TicketIssueJobParams
    ) -> tuple[ClaudeResponse, TicketIssuePlan]:
        self.call_count += 1
        if self.raise_exc:
            raise self.raise_exc
        assert self.plan is not None
        response = ClaudeResponse(
            model="claude-sonnet-4-6",
            stop_reason="tool_use",
            input_tokens=1000,
            output_tokens=300,
        )
        return response, self.plan


@dataclass
class FakeGitHub:
    existing_issues: list[dict] = field(default_factory=list)        # child-marker search hits
    existing_parent: list[dict] = field(default_factory=list)        # parent-marker search hits
    created: list[dict] = field(default_factory=list)
    sub_issues: list[tuple[int, int]] = field(default_factory=list)  # (parent_number, sub_issue_id)
    labels_ensured: list[str] = field(default_factory=list)
    installation_exc: Exception | None = None
    create_exc_on_call: int | None = None  # 1-based index of create_issue call that raises
    installation_calls: int = 0
    search_calls: int = 0
    _create_calls: int = 0
    next_number: int = 100

    def get_repo_installation_id(self, owner: str, repo: str) -> int:
        self.installation_calls += 1
        if self.installation_exc:
            raise self.installation_exc
        return 555

    def get_installation_token(self, installation_id: int) -> str:
        assert installation_id == 555
        return "tok"

    def find_issues_with_marker(self, token, owner, repo, marker) -> list[dict]:
        self.search_calls += 1
        # the parent carries an extra, ticket-specific "revaticketparent<digest>" token
        if "parent" in marker:
            return list(self.existing_parent)
        return list(self.existing_issues)

    def ensure_label(self, token, owner, repo, name, color="5319e7", description="") -> None:
        self.labels_ensured.append(name)

    def create_issue(self, token, owner, repo, title, body, labels=None) -> dict:
        self._create_calls += 1
        if self.create_exc_on_call == self._create_calls:
            raise PermanentError("GitHub 403 secondary rate limit")
        self.next_number += 1
        self.created.append(
            {"owner": owner, "repo": repo, "title": title, "body": body, "labels": labels,
             "number": self.next_number}
        )
        return {
            "number": self.next_number,
            "url": f"https://github.com/{owner}/{repo}/issues/{self.next_number}",
            "id": 900_000 + self.next_number,
        }

    def add_sub_issue(self, token, owner, repo, parent_number, sub_issue_id) -> None:
        self.sub_issues.append((parent_number, sub_issue_id))


@dataclass
class FakeOdoo:
    raise_exc: Exception | None = None
    calls: list[dict] = field(default_factory=list)

    def issues_created(self, ticket_id, model_name, request_id, status, issues, error=None):
        self.calls.append(
            {"ticket_id": ticket_id, "model_name": model_name, "request_id": request_id,
             "status": status, "issues": issues, "error": error}
        )
        if self.raise_exc:
            raise self.raise_exc

    def issue_state(self, ticket_id, model_name, number, state, issues):
        self.calls.append(
            {"ticket_id": ticket_id, "model_name": model_name, "number": number,
             "state": state, "issues": issues}
        )
        if self.raise_exc:
            raise self.raise_exc


# --- Helpers -----------------------------------------------------------------


def _plan(n: int = 2) -> TicketIssuePlan:
    return TicketIssuePlan(
        issues=[
            TicketIssueItem(
                title=f"Issue {i}",
                body=f"Body {i}",
                acceptance_criteria=[f"criterion {i}"],
            )
            for i in range(1, n + 1)
        ]
    )


@pytest.fixture()
def ctx_and_fakes():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)

    planner = FakePlanner(plan=_plan())
    github = FakeGitHub()
    odoo = FakeOdoo()
    ctx = WorkerContext(
        db=db,
        claude=None,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        reviewer=None,  # type: ignore[arg-type]
        auditor=None,  # type: ignore[arg-type]
        ticket_analyzer=None,  # type: ignore[arg-type]
        verifier=None,  # type: ignore[arg-type]
        odoo=odoo,  # type: ignore[arg-type]
        ticket_issue_planner=planner,  # type: ignore[arg-type]
    )
    set_context(ctx)
    return {"ctx": ctx, "db": db, "planner": planner, "github": github, "odoo": odoo}


def _make_params(db: Database) -> dict:
    stub = TicketIssueJobParams(
        run_id=0,
        ticket_id=123,
        model_name="helpdesk.ticket",
        github_url="https://github.com/acme/widgets",
        name="Login page broken",
        description="We need a login page.",
        analysis_html="<h2>Summary</h2>",
        priority="1",
        ticket_url="https://odoo.example.com/web#id=123&model=helpdesk.ticket&view_type=form",
    )
    run_id = writers.record_ticket_issue_run_created(db, stub)
    writers.attach_ticket_issue_job_id(db, run_id, f"rq:job:ti-{run_id}")
    return stub.model_copy(update={"run_id": run_id}).model_dump()


# --- Tests -------------------------------------------------------------------


def test_happy_path_creates_issues_and_calls_back(ctx_and_fakes):
    s = ctx_and_fakes
    params = _make_params(s["db"])

    out = run_ticket_issues(params)

    assert out["status"] == "completed"
    assert s["planner"].call_count == 1
    assert s["github"].labels_ensured == ["reva-ticket"]
    # 1 parent (index 0) + 2 children
    assert len(s["github"].created) == 3
    body = s["github"].created[1]["body"]  # index 0 is now the parent
    assert "Body 1" in body
    assert "- [ ] criterion 1" in body
    # mandatory Odoo back-link + hidden ticket-level dedup marker
    assert params["ticket_url"] in body
    assert "<!-- revaticket" in body
    assert s["github"].created[1]["labels"] == ["reva-ticket"]

    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["status"] == "completed"
    assert [i["number"] for i in row["issues"]] == [102, 103]
    assert row["model"] == "claude-sonnet-4-6"
    assert row["estimated_cost_usd"] > 0

    assert len(s["odoo"].calls) == 1
    cb = s["odoo"].calls[0]
    assert cb["status"] == "created"
    assert cb["request_id"] == params["run_id"]
    # Titles carry the Odoo record id and the implementation order (n/total)
    assert cb["issues"] == [
        {"number": 102, "title": "[Ticket 123] 1/2 — Issue 1",
         "url": "https://github.com/acme/widgets/issues/102"},
        {"number": 103, "title": "[Ticket 123] 2/2 — Issue 2",
         "url": "https://github.com/acme/widgets/issues/103"},
    ]


def test_two_issues_creates_parent_and_attaches_children(ctx_and_fakes):
    s = ctx_and_fakes
    params = _make_params(s["db"])

    out = run_ticket_issues(params)
    assert out["status"] == "completed"

    # 1 parent + 2 children created
    assert len(s["github"].created) == 3
    parent_create = s["github"].created[0]
    assert parent_create["title"] == "[Ticket 123] Login page broken"   # _parent_title
    assert "<!-- revaticketparent" in parent_create["body"]              # parent-only tag
    assert "<!-- revaticket" in parent_create["body"]                    # shared marker too

    # both children attached to the parent (number 101), by their database id
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    pnum = row["parent_issue"]["number"]
    assert pnum == 101
    assert sorted(s["github"].sub_issues) == [(101, 900_000 + 102), (101, 900_000 + 103)]
    assert all(i["attached"] for i in row["issues"])

    # Odoo callback carries ONLY the children, no parent
    cb = s["odoo"].calls[0]
    assert [i["number"] for i in cb["issues"]] == [102, 103]
    assert all(i["number"] != pnum for i in cb["issues"])


def test_single_issue_creates_no_parent(ctx_and_fakes):
    s = ctx_and_fakes
    s["planner"].plan = _plan(1)
    params = _make_params(s["db"])

    out = run_ticket_issues(params)
    assert out["status"] == "completed"
    assert len(s["github"].created) == 1            # no parent
    assert s["github"].sub_issues == []
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["parent_issue"] is None
    assert len(s["odoo"].calls[0]["issues"]) == 1


def test_resume_reattaches_only_unattached_children(ctx_and_fakes):
    """A requeue after children were created but before all were attached must
    not duplicate the parent or re-create children — only finish the attaching."""
    s = ctx_and_fakes
    # first run: child #2 attach is the failure point — simulate by failing the
    # SECOND create so only one child exists, then requeue.
    s["github"].create_exc_on_call = 3   # parent(1) + child1(2) ok, child2(3) raises
    params = _make_params(s["db"])
    with pytest.raises(PermanentError):
        run_ticket_issues(params)

    writers.reset_ticket_issue_run(s["db"], params["run_id"])
    s["github"].create_exc_on_call = None
    created_before = len(s["github"].created)

    out = run_ticket_issues(params)
    assert out["status"] == "completed"
    # parent (already created) NOT re-created; only the missing child is created
    assert len(s["github"].created) == created_before + 1
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert all(i["attached"] for i in row["issues"])
    assert len(s["github"].sub_issues) == 2          # both children end up attached


def test_issue_title_format():
    from worker.ticket_issue_runner import _issue_title

    params = TicketIssueJobParams(
        run_id=1, ticket_id=2010, model_name="project.task",
        github_url="https://github.com/acme/widgets", name="n", description="d",
        analysis_html="", priority="1", ticket_url="https://odoo.example.com/web#id=2010",
    )
    assert _issue_title(params, 3, 10, "Implement login form") == \
        "[Task 2010] 3/10 — Implement login form"


def test_spend_recorded_in_ledger(ctx_and_fakes):
    from datetime import datetime, timedelta, timezone

    s = ctx_and_fakes
    run_ticket_issues(_make_params(s["db"]))
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert writers.sum_estimated_cost_since(s["db"], since) > 0


def test_reconcile_existing_issues_skips_planning_and_creation(ctx_and_fakes):
    """Re-run for a ticket that already has marked issues (timeout race or
    re-click): re-link them via the callback instead of duplicating."""
    s = ctx_and_fakes
    s["github"].existing_issues = [
        {"number": 7, "title": "Old issue", "url": "https://github.com/acme/widgets/issues/7"},
    ]
    params = _make_params(s["db"])

    out = run_ticket_issues(params)

    assert out["status"] == "completed"
    assert s["planner"].call_count == 0
    assert s["github"].created == []
    cb = s["odoo"].calls[0]
    assert cb["status"] == "created"
    assert cb["issues"] == s["github"].existing_issues
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["status"] == "completed"


def test_app_not_installed_fails_run_and_sends_failed_callback(ctx_and_fakes):
    s = ctx_and_fakes
    s["github"].installation_exc = PermanentError("GitHub app not installed (404)")
    params = _make_params(s["db"])

    with pytest.raises(PermanentError):
        run_ticket_issues(params)

    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["status"] == "failed"
    assert "not installed" in row["error_message"]
    cb = s["odoo"].calls[0]
    assert cb["status"] == "failed"
    assert cb["issues"] == []
    assert cb["request_id"] == params["run_id"]
    assert "not installed" in cb["error"]


def test_transient_planner_error_also_fails_and_calls_back(ctx_and_fakes):
    """Unlike ticket analysis, transient errors mark the run failed AND send the
    failed callback: no RQ retry is configured for this pipeline, and Odoo can
    only leave 'pending' (button hidden) via a callback."""
    s = ctx_and_fakes
    s["planner"].raise_exc = TransientError("Claude 529")
    params = _make_params(s["db"])

    with pytest.raises(TransientError):
        run_ticket_issues(params)

    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["status"] == "failed"
    assert s["odoo"].calls[0]["status"] == "failed"


def test_partial_failure_persists_progress_then_requeue_resumes(ctx_and_fakes):
    s = ctx_and_fakes
    # call 1 = parent, call 2 = child 1 (ok), call 3 = child 2 (raises)
    s["github"].create_exc_on_call = 3
    params = _make_params(s["db"])

    with pytest.raises(PermanentError):
        run_ticket_issues(params)

    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["status"] == "failed"
    assert row["issues"][0]["number"] == 102  # first child persisted (parent took 101)
    assert row["issues"][1]["number"] is None
    assert s["odoo"].calls[-1]["status"] == "failed"

    # ops requeue: reset (keeps the plan) and re-run — resumes, never re-plans
    writers.reset_ticket_issue_run(s["db"], params["run_id"])
    s["github"].create_exc_on_call = None
    planner_calls_before = s["planner"].call_count

    out = run_ticket_issues(params)

    assert out["status"] == "completed"
    assert s["planner"].call_count == planner_calls_before  # no re-plan
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert [i["number"] for i in row["issues"]] == [102, 103]
    cb = s["odoo"].calls[-1]
    assert cb["status"] == "created"
    assert len(cb["issues"]) == 2  # FULL set, not just the resumed one


def test_permanent_callback_error_after_creation_is_swallowed(ctx_and_fakes):
    """A 409 from Odoo (timeout race / stale request_id) is do-not-retry per
    the contract and must not undo the run — the issues exist and are
    persisted, so the job completes (re-raising would only trigger useless RQ
    retries that 409 again)."""
    s = ctx_and_fakes
    s["odoo"].raise_exc = PermanentError("Odoo /issues-created 409 (permanent)")
    params = _make_params(s["db"])

    out = run_ticket_issues(params)

    assert out["status"] == "completed"
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["status"] == "completed"
    assert [i["number"] for i in row["issues"]] == [102, 103]  # parent took 101


def test_transient_callback_error_after_creation_reraises_for_rq_retry(ctx_and_fakes):
    """Contract 2: 5xx/network on the callback must be retried (the api
    enqueues with rq.Retry). The job re-raises so RQ reruns it; the rerun
    short-circuits and just re-sends the callback."""
    s = ctx_and_fakes
    s["odoo"].raise_exc = TransientError("Odoo 503")
    params = _make_params(s["db"])

    with pytest.raises(TransientError):
        run_ticket_issues(params)

    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["status"] == "completed"  # work is done; only the callback is owed

    # the RQ retry reruns the job: no new GitHub setup, no creates, callback re-sent
    s["odoo"].raise_exc = None
    installation_calls_before = s["github"].installation_calls
    out = run_ticket_issues(params)
    assert out["status"] == "completed"
    assert s["github"].installation_calls == installation_calls_before  # short-circuit
    assert len(s["github"].created) == 3  # nothing re-created (parent + 2 children)
    assert s["odoo"].calls[-1]["status"] == "created"
    assert len(s["odoo"].calls[-1]["issues"]) == 2


def test_transient_error_with_retries_remaining_keeps_row_pending(ctx_and_fakes, monkeypatch):
    """While RQ retries remain, a transient failure leaves the row pending and
    sends no callback — the retry resumes; the failed callback goes out only on
    the final attempt."""

    class _Job:
        retries_left = 2

    monkeypatch.setattr("worker.ticket_issue_runner.get_current_job", lambda: _Job())
    s = ctx_and_fakes
    s["planner"].raise_exc = TransientError("Claude 529")
    params = _make_params(s["db"])

    with pytest.raises(TransientError):
        run_ticket_issues(params)

    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["status"] == "pending"
    assert s["odoo"].calls == []


def test_reclick_adopts_prior_plan_and_creates_missing(ctx_and_fakes):
    """Re-click after a partial failure: the fresh run adopts the prior run's
    persisted plan from OUR DB (authoritative, includes the un-created
    remainder) instead of trusting GitHub's eventually-consistent search."""
    s = ctx_and_fakes
    # prior run: planned 2, created 1, failed
    prior = _make_params(s["db"])
    prior_plan = [
        {"title": "Issue 1", "body": "Body 1", "acceptance_criteria": [],
         "number": 55, "url": "https://github.com/acme/widgets/issues/55",
         "id": 900_055, "attached": True},
        {"title": "Issue 2", "body": "Body 2", "acceptance_criteria": [],
         "number": None, "url": None, "id": None, "attached": False},
    ]
    writers.update_ticket_issue_progress(s["db"], prior["run_id"], prior_plan)
    writers.record_ticket_issue_run_failed(s["db"], prior["run_id"], "GitHub 403")

    fresh = _make_params(s["db"])  # the re-click
    out = run_ticket_issues(fresh)

    assert out["status"] == "completed"
    assert s["planner"].call_count == 0  # no re-plan
    assert s["github"].search_calls == 0  # DB beat the search
    assert len(s["github"].created) == 2  # parent + the missing child
    cb = s["odoo"].calls[-1]
    assert cb["request_id"] == fresh["run_id"]
    # full set: kept child (#55) + newly-created child. The parent is created
    # FIRST and takes #101, so the new child takes #102 (the brief's [55, 101]
    # predates the parent-first ordering — see report).
    assert [i["number"] for i in cb["issues"]] == [55, 102]


def test_prior_plan_for_different_repo_is_not_adopted(ctx_and_fakes):
    s = ctx_and_fakes
    prior = _make_params(s["db"])
    plan = [{"title": "Old", "body": "b", "acceptance_criteria": [],
             "number": 7, "url": "https://github.com/other/repo/issues/7"}]
    from reva.db.models import TicketIssueRun
    with s["db"].session() as session:
        row = session.get(TicketIssueRun, prior["run_id"])
        row.github_url = "https://github.com/other/repo"
        row.issues = plan
        row.status = "failed"

    fresh = _make_params(s["db"])
    run_ticket_issues(fresh)

    # different repo -> prior plan ignored, normal plan+create path
    assert s["planner"].call_count == 1
    assert len(s["github"].created) == 3  # parent + 2 children


def test_marker_is_stable_across_url_casing():
    from worker.ticket_issue_runner import _ticket_marker

    assert _ticket_marker("Acme", "Widgets", "helpdesk.ticket", 7, "abc123") == \
        _ticket_marker("acme", "widgets", "helpdesk.ticket", 7, "abc123")
    # a changed planning basis changes the key — old issues are deliberately
    # not reconciled when the spec changed
    assert _ticket_marker("acme", "widgets", "helpdesk.ticket", 7, "abc123") != \
        _ticket_marker("acme", "widgets", "helpdesk.ticket", 7, "def456")


def test_changed_description_prevents_stale_plan_adoption(ctx_and_fakes):
    """A revised planning basis (edited description / new consultant docx)
    must re-plan, not adopt the prior run's stale plan. The basis is stored
    at row creation, so the fresh run must be created WITH the new text."""
    s = ctx_and_fakes
    prior = _make_params(s["db"])
    prior_plan = [{"title": "Old split", "body": "b", "acceptance_criteria": [],
                   "number": 55, "url": "https://github.com/acme/widgets/issues/55"}]
    writers.update_ticket_issue_progress(s["db"], prior["run_id"], prior_plan)
    writers.record_ticket_issue_run_failed(s["db"], prior["run_id"], "boom")

    revised = TicketIssueJobParams(
        run_id=0, ticket_id=123, model_name="helpdesk.ticket",
        github_url="https://github.com/acme/widgets", name="Login page broken",
        description="A completely revised requirement.", analysis_html="",
        priority="1", ticket_url="https://odoo.example.com/web#id=123",
    )
    fresh_id = writers.record_ticket_issue_run_created(s["db"], revised)
    out = run_ticket_issues(revised.model_copy(update={"run_id": fresh_id}).model_dump())

    assert out["status"] == "completed"
    assert s["planner"].call_count == 1  # re-planned from the new basis
    assert len(s["github"].created) == 3  # fresh set (parent + 2), old plan not adopted


def test_failed_callback_error_never_masks_original_error(ctx_and_fakes):
    s = ctx_and_fakes
    s["planner"].raise_exc = PermanentError("schema validation failed")
    s["odoo"].raise_exc = TransientError("Odoo down")
    params = _make_params(s["db"])

    with pytest.raises(PermanentError, match="schema validation"):
        run_ticket_issues(params)

    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["status"] == "failed"


def test_invalid_github_url_is_permanent(ctx_and_fakes):
    s = ctx_and_fakes
    params = _make_params(s["db"])
    params["github_url"] = "https://gitlab.com/acme/widgets"

    with pytest.raises(PermanentError, match="github_url"):
        run_ticket_issues(params)

    assert s["odoo"].calls[0]["status"] == "failed"


# --- sync_ticket_issue_state -----------------------------------------------------


def _seed_completed_run(s) -> dict:
    """A completed run for acme/widgets with two open child issues (102, 103).

    The parent ("epic") took number 101 — children follow it.
    """
    params = _make_params(s["db"])
    run_ticket_issues(params)
    return params


def test_issue_closed_updates_db_and_notifies_odoo(ctx_and_fakes):
    from worker.ticket_issue_runner import sync_ticket_issue_state

    s = ctx_and_fakes
    params = _seed_completed_run(s)
    s["odoo"].calls.clear()

    out = sync_ticket_issue_state(
        {"owner": "Acme", "repo": "Widgets", "number": 102, "state": "closed"}
    )

    assert out == {"status": "completed", "records": 1, "notified": 1}
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["issues"][0]["state"] == "closed"
    assert row["issues"][1]["state"] == "open"

    cb = s["odoo"].calls[0]
    assert cb["ticket_id"] == 123 and cb["model_name"] == "helpdesk.ticket"
    assert cb["number"] == 102 and cb["state"] == "closed"
    assert cb["issues"] == [
        {"number": 102, "title": "[Ticket 123] 1/2 — Issue 1",
         "url": "https://github.com/acme/widgets/issues/102", "state": "closed"},
        {"number": 103, "title": "[Ticket 123] 2/2 — Issue 2",
         "url": "https://github.com/acme/widgets/issues/103", "state": "open"},
    ]


def test_issue_reopened_syncs_back_to_open(ctx_and_fakes):
    from worker.ticket_issue_runner import sync_ticket_issue_state

    s = ctx_and_fakes
    params = _seed_completed_run(s)
    sync_ticket_issue_state({"owner": "acme", "repo": "widgets", "number": 102, "state": "closed"})
    sync_ticket_issue_state({"owner": "acme", "repo": "widgets", "number": 102, "state": "open"})

    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["issues"][0]["state"] == "open"
    assert s["odoo"].calls[-1]["state"] == "open"


def test_issue_state_no_match_is_noop(ctx_and_fakes):
    from worker.ticket_issue_runner import sync_ticket_issue_state

    s = ctx_and_fakes
    _seed_completed_run(s)
    s["odoo"].calls.clear()

    out = sync_ticket_issue_state(
        {"owner": "acme", "repo": "widgets", "number": 999, "state": "closed"}
    )
    assert out == {"status": "no_match"}
    assert s["odoo"].calls == []


def test_issue_state_odoo_409_is_swallowed_db_still_updated(ctx_and_fakes):
    from worker.ticket_issue_runner import sync_ticket_issue_state

    s = ctx_and_fakes
    params = _seed_completed_run(s)
    s["odoo"].raise_exc = PermanentError("Odoo /issue-state 409 (permanent)")

    out = sync_ticket_issue_state(
        {"owner": "acme", "repo": "widgets", "number": 102, "state": "closed"}
    )

    assert out["notified"] == 0
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["issues"][0]["state"] == "closed"  # DB state recorded regardless


def test_issue_state_odoo_transient_reraises_for_retry(ctx_and_fakes):
    from worker.ticket_issue_runner import sync_ticket_issue_state

    s = ctx_and_fakes
    _seed_completed_run(s)
    s["odoo"].raise_exc = TransientError("Odoo down")

    with pytest.raises(TransientError):
        sync_ticket_issue_state(
            {"owner": "acme", "repo": "widgets", "number": 102, "state": "closed"}
        )
