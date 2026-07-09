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
    reject_assignees: bool = False
    installation_calls: int = 0
    search_calls: int = 0
    _create_calls: int = 0
    next_number: int = 100
    # --- Projects v2 fakes (defaults keep pre-feature tests untouched) -------
    project_fields: list[dict] = field(default_factory=lambda: [
        {"id": "F_status", "name": "Status", "dataType": "SINGLE_SELECT",
         "options": [{"id": "opt_todo", "name": "Todo"},
                     {"id": "opt_done", "name": "Done"}]},
    ])
    project_exc: Exception | None = None          # raised by get_project
    set_date_exc: Exception | None = None         # raised by set_project_item_date
    get_project_calls: int = 0
    project_items: list[str] = field(default_factory=list)      # content node_ids added
    item_field_sets: list[tuple] = field(default_factory=list)  # (item_id, field_id, value)
    created_fields: list[dict] = field(default_factory=list)
    issue_nodes: dict[int, str] = field(default_factory=dict)   # number → node_id (backfill)

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

    def create_issue(
        self, token, owner, repo, title, body, labels=None, assignees=None
    ) -> dict:
        self._create_calls += 1
        if self.create_exc_on_call == self._create_calls:
            raise PermanentError("GitHub 403 secondary rate limit")
        if assignees and self.reject_assignees:
            raise PermanentError("GitHub 422 assignee is not assignable")
        self.next_number += 1
        self.created.append(
            {"owner": owner, "repo": repo, "title": title, "body": body, "labels": labels,
             "assignees": assignees, "number": self.next_number}
        )
        return {
            "number": self.next_number,
            "url": f"https://github.com/{owner}/{repo}/issues/{self.next_number}",
            "id": 900_000 + self.next_number,
            "node_id": f"I_{self.next_number}",
        }

    def add_sub_issue(self, token, owner, repo, parent_number, sub_issue_id) -> None:
        self.sub_issues.append((parent_number, sub_issue_id))

    # --- Projects v2 methods --------------------------------------------------

    def get_project(self, token, owner_type, owner, number):
        self.get_project_calls += 1
        if self.project_exc:
            raise self.project_exc
        return {"id": "P_1", "fields": list(self.project_fields)}

    def create_project_field(self, token, project_id, name, data_type, options=None):
        f = {"id": f"F_{name}", "name": name, "dataType": data_type,
             "options": [{"id": f"opt_{o['name'].lower()}", "name": o["name"]}
                         for o in options or []]}
        self.created_fields.append(f)
        return f

    def add_issue_to_project(self, token, project_id, content_node_id):
        self.project_items.append(content_node_id)
        return f"PVTI_{content_node_id}"

    def set_project_item_date(self, token, project_id, item_id, field_id, date_value):
        if self.set_date_exc:
            raise self.set_date_exc
        self.item_field_sets.append((item_id, field_id, date_value))

    def set_project_item_option(self, token, project_id, item_id, field_id, option_id):
        self.item_field_sets.append((item_id, field_id, option_id))

    def get_issue(self, token, owner, repo, number):
        node = self.issue_nodes.get(number)
        return {"title": "t", "body": "b", "node_id": node} if node else None


@dataclass
class FakeOdoo:
    raise_exc: Exception | None = None
    ready_raise_exc: Exception | None = None
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

    def tickets_ready(self, ticket_id, model_name, issues):
        self.calls.append(
            {"ticket_id": ticket_id, "model_name": model_name, "ready": True,
             "issues": issues}
        )
        if self.ready_raise_exc:
            raise self.ready_raise_exc


# --- Helpers -----------------------------------------------------------------


def _plan(n: int = 2) -> TicketIssuePlan:
    return TicketIssuePlan(
        issues=[
            TicketIssueItem(
                title=f"Issue {i}",
                body=f"Body {i}",
                acceptance_criteria=[f"criterion {i}"],
                estimate_hours=1.5,
            )
            for i in range(1, n + 1)
        ]
    )


@pytest.fixture()
def ctx_and_fakes(monkeypatch):
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
        ticket_issue_planner=planner,  # type: ignore[arg-type]
    )
    monkeypatch.setattr("worker.ticket_issue_runner.build_odoo_client", lambda ctx, _id: odoo)
    set_context(ctx)
    return {"ctx": ctx, "db": db, "planner": planner, "github": github, "odoo": odoo}


def _make_params(db: Database, **overrides) -> dict:
    stub = TicketIssueJobParams(**{**dict(
        run_id=0, odoo_instance_id=1, ticket_id=123, model_name="helpdesk.ticket",
        github_url="https://github.com/acme/widgets", name="Login page broken",
        description="We need a login page.", analysis_html="<h2>Summary</h2>",
        priority="1",
        ticket_url="https://odoo.example.com/web#id=123&model=helpdesk.ticket&view_type=form",
    ), **overrides})
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
    assert s["github"].labels_ensured == ["reva-ticket", "DEV"]
    # 1 parent (index 0) + 2 children
    assert len(s["github"].created) == 3
    body = s["github"].created[1]["body"]  # index 0 is now the parent
    assert "Body 1" in body
    assert "- [ ] criterion 1" in body
    # mandatory Odoo back-link + hidden ticket-level dedup marker
    assert params["ticket_url"] in body
    assert "<!-- revaticket" in body
    assert s["github"].created[1]["labels"] == ["reva-ticket", "DEV"]

    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["status"] == "completed"
    assert [i["number"] for i in row["issues"]] == [102, 103]
    assert row["model"] == "claude-sonnet-4-6"
    assert row["estimated_cost_usd"] > 0

    # No project URL → zero Projects interaction (guards accidental coupling)
    assert s["github"].get_project_calls == 0
    assert s["github"].project_items == []

    assert len(s["odoo"].calls) == 1
    cb = s["odoo"].calls[0]
    assert cb["status"] == "created"
    assert cb["request_id"] == params["run_id"]
    # Titles carry the Odoo record id and the implementation order (n/total)
    assert cb["issues"] == [
        {"number": 102, "title": "[DEV] 123 - Issue 1 (1/2)",
         "url": "https://github.com/acme/widgets/issues/102", "state": "open",
         "plan_date": None, "complete_date": None},
        {"number": 103, "title": "[DEV] 123 - Issue 2 (2/2)",
         "url": "https://github.com/acme/widgets/issues/103", "state": "open",
         "plan_date": None, "complete_date": None},
    ]


def test_placeholder_plan_copies_ticket_into_issue_body(ctx_and_fakes):
    s = ctx_and_fakes
    s["planner"].plan = TicketIssuePlan(issues=[
        TicketIssueItem(title="placeholder", body="placeholder")
    ])
    params = _make_params(
        s["db"],
        name="test",
        description="Customer says the approval button is missing on project tasks.",
        analysis_html="<h2>Summary</h2><p>Need an approval action.</p>",
    )

    run_ticket_issues(params)

    child = s["github"].created[0]
    assert child["title"] == "[DEV] 123 - test"
    assert "placeholder" not in child["body"].lower()
    assert "Customer says the approval button is missing" in child["body"]
    assert "Need an approval action" in child["body"]


def test_github_username_assigns_created_issues(ctx_and_fakes):
    s = ctx_and_fakes
    params = _make_params(s["db"], github_username="jane-doe")

    run_ticket_issues(params)

    assert [issue["assignees"] for issue in s["github"].created] == [
        ["jane-doe"],
        ["jane-doe"],
        ["jane-doe"],
    ]
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["github_username"] == "jane-doe"


def test_github_assignee_422_retries_without_assignee(ctx_and_fakes):
    s = ctx_and_fakes
    s["github"].reject_assignees = True
    params = _make_params(s["db"], github_username="jane-doe")

    out = run_ticket_issues(params)

    assert out["status"] == "completed"
    assert len(s["github"].created) == 3
    assert [issue["assignees"] for issue in s["github"].created] == [None, None, None]


def test_two_issues_creates_parent_and_attaches_children(ctx_and_fakes):
    s = ctx_and_fakes
    params = _make_params(s["db"])

    out = run_ticket_issues(params)
    assert out["status"] == "completed"

    # 1 parent + 2 children created
    assert len(s["github"].created) == 3
    parent_create = s["github"].created[0]
    assert parent_create["title"] == "[DEV] 123 - Login page broken"   # _parent_title
    assert "<!-- revaticketparent" in parent_create["body"]              # parent-only tag
    assert "<!-- revaticket" in parent_create["body"]                    # shared marker too
    assert "### Summary" in parent_create["body"]                        # ticket summary
    assert "We need a login page." in parent_create["body"]

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


def test_parent_body_summary_falls_back_to_analysis():
    """The epic body carries a ticket summary: description when present, else
    the REVA analysis (HTML stripped); omitted entirely when neither exists."""
    from worker.ticket_issue_runner import _format_parent_body

    def body(**over):
        p = TicketIssueJobParams(**{**dict(
            run_id=1, odoo_instance_id=1, ticket_id=123, model_name="helpdesk.ticket",
            github_url="https://github.com/acme/widgets", name="Login",
            description="We need a login page.", analysis_html="<h2>Analysis</h2>",
            priority="1", ticket_url="https://odoo.example.com/web#id=123",
        ), **over})
        return _format_parent_body(p, "revaticketX", "revaticketparentX")

    assert "### Summary\n\nWe need a login page." in body()
    # description empty → fall back to the analysis (HTML stripped to text)
    assert "### Summary" in body(description="", analysis_html="<p>Root cause: X</p>")
    assert "Root cause: X" in body(description="", analysis_html="<p>Root cause: X</p>")
    # neither present → no Summary section
    assert "### Summary" not in body(description="", analysis_html="")


def test_estimate_rendered_on_issue_and_epic(ctx_and_fakes):
    """Each child issue body shows its low-end estimate; the epic shows the
    total across issues (2 × 1.5 h = 3 h)."""
    s = ctx_and_fakes
    params = _make_params(s["db"])
    run_ticket_issues(params)

    created = {c["title"]: c for c in s["github"].created}
    child = next(c for t, c in created.items() if "1/2" in t)
    assert "**Estimate:** ~1.5 h" in child["body"]
    parent = s["github"].created[0]                     # parent created first
    assert "**Estimated effort:** ~3 h across 2 issues" in parent["body"]


def test_single_issue_stays_flat(ctx_and_fakes):
    """A single planned issue is already the ticket work item; no parent."""
    s = ctx_and_fakes
    s["planner"].plan = _plan(1)
    params = _make_params(s["db"])

    out = run_ticket_issues(params)
    assert out["status"] == "completed"
    assert len(s["github"].created) == 1
    assert s["github"].created[0]["title"] == "[DEV] 123 - Issue 1"
    assert "<!-- revaticketparent" not in s["github"].created[0]["body"]
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["parent_issue"] is None
    assert s["github"].sub_issues == []
    assert [i["number"] for i in s["odoo"].calls[0]["issues"]] == [101]


def test_second_run_attaches_to_existing_epic_and_sends_union(ctx_and_fakes):
    s = ctx_and_fakes
    params1 = _make_params(s["db"])
    run_ticket_issues(params1)                       # parent 101 + children 102, 103
    parent_number = s["github"].created[0]["number"]
    assert parent_number == 101

    s["planner"].plan = TicketIssuePlan(issues=[
        TicketIssueItem(title="Adjust layout", body="B", type="CR")])
    params2 = _make_params(s["db"], issue_type="CR", description="Change the layout")
    run_ticket_issues(params2)

    # no second parent: exactly one new GitHub issue, attached to run 1's epic
    assert len(s["github"].created) == 4
    assert s["github"].created[3]["title"] == "[CR] 123 - Adjust layout"
    assert s["github"].sub_issues[-1] == (101, 900104)

    # the second callback carries the UNION of both runs' issues, with state
    cb = s["odoo"].calls[-1]
    assert [i["number"] for i in cb["issues"]] == [102, 103, 104]
    assert all(i["state"] == "open" for i in cb["issues"])


def test_single_issue_without_epic_creates_no_parent(ctx_and_fakes):
    s = ctx_and_fakes
    s["planner"].plan = TicketIssuePlan(issues=[
        TicketIssueItem(title="One thing", body="B")])
    params = _make_params(s["db"])
    run_ticket_issues(params)
    assert len(s["github"].created) == 1
    assert s["github"].created[0]["title"] == "[DEV] 123 - One thing"
    assert len(s["github"].sub_issues) == 0


def test_state_sync_sends_union_snapshot(ctx_and_fakes):
    s = ctx_and_fakes
    params1 = _make_params(s["db"])
    run_ticket_issues(params1)                       # issues 102, 103
    s["planner"].plan = TicketIssuePlan(issues=[
        TicketIssueItem(title="Adjust layout", body="B", type="CR")])
    params2 = _make_params(s["db"], issue_type="CR", description="Change the layout")
    run_ticket_issues(params2)                       # issue 104
    s["odoo"].calls.clear()

    from worker.ticket_issue_runner import sync_ticket_issue_state
    sync_ticket_issue_state({"owner": "acme", "repo": "widgets", "number": 102, "state": "closed"})

    cb = s["odoo"].calls[0]
    numbers = {i["number"]: i["state"] for i in cb["issues"]}
    assert numbers == {102: "closed", 103: "open", 104: "open"}


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


def test_typed_single_issue_title_and_labels(ctx_and_fakes):
    s = ctx_and_fakes
    s["planner"].plan = TicketIssuePlan(issues=[TicketIssueItem(
        title="Adjust delivery slip layout that is way too long for a title",
        body="B", type="FEAT")])
    params = _make_params(s["db"], issue_type="CR", description="Change the layout")

    run_ticket_issues(params)

    created = s["github"].created
    assert len(created) == 1
    # request type CR overrides the planner's FEAT; tldr truncated to 30; no (n/total)
    assert created[0]["title"] == "[CR] 123 - Adjust delivery slip layout th"
    assert created[0]["labels"] == ["reva-ticket", "CR"]
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["issues"][0]["type"] == "CR"


def test_mixed_types_title_sequence_and_dominant_parent(ctx_and_fakes):
    s = ctx_and_fakes
    s["planner"].plan = TicketIssuePlan(issues=[
        TicketIssueItem(title="Add report", body="B", type="FEAT"),
        TicketIssueItem(title="Refactor flow", body="B", type="DEV"),
        TicketIssueItem(title="Add second report", body="B", type="FEAT"),
    ])
    params = _make_params(s["db"])

    run_ticket_issues(params)

    titles = [c["title"] for c in s["github"].created]
    assert titles[0] == "[FEAT] 123 - Login page broken"       # parent, dominant type
    assert titles[1] == "[FEAT] 123 - Add report (1/3)"
    assert titles[2] == "[DEV] 123 - Refactor flow (2/3)"
    assert titles[3] == "[FEAT] 123 - Add second report (3/3)"
    assert s["github"].created[0]["labels"] == ["reva-ticket", "FEAT"]
    assert sorted(s["github"].labels_ensured) == ["DEV", "FEAT", "reva-ticket"]


def test_issue_title_format():
    from worker.ticket_issue_runner import _issue_title

    params = TicketIssueJobParams(
        run_id=1, odoo_instance_id=1, ticket_id=2010, model_name="project.task",
        github_url="https://github.com/acme/widgets", name="n", description="d",
        analysis_html="", priority="1", ticket_url="https://odoo.example.com/web#id=2010",
    )
    assert _issue_title(params, 3, 10, "Implement login form", "CR") == \
        "[CR] 2010 - Implement login form (3/10)"


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
    # union normalizes every item with a state (defaults "open") + per-issue dates
    assert cb["issues"] == [{**s["github"].existing_issues[0], "state": "open",
                             "plan_date": None, "complete_date": None}]
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


def test_legacy_prior_plan_without_id_creates_no_parent(ctx_and_fakes):
    """Pre-feature prior plan: children have numbers but no id/attached. A
    re-click must NOT backfill a parent (would be an empty epic) — stays flat."""
    s = ctx_and_fakes
    prior = _make_params(s["db"])
    prior_plan = [
        {"title": "A", "body": "b", "acceptance_criteria": [], "number": 70,
         "url": "https://github.com/acme/widgets/issues/70"},
        {"title": "B", "body": "b", "acceptance_criteria": [], "number": 71,
         "url": "https://github.com/acme/widgets/issues/71"},
    ]
    writers.update_ticket_issue_progress(s["db"], prior["run_id"], prior_plan)
    writers.record_ticket_issue_run_failed(s["db"], prior["run_id"], "x")

    fresh = _make_params(s["db"])
    out = run_ticket_issues(fresh)
    assert out["status"] == "completed"
    assert s["github"].created == []
    assert s["github"].sub_issues == []
    row = writers.get_ticket_issue_run(s["db"], fresh["run_id"])
    assert row["parent_issue"] is None
    assert [i["number"] for i in s["odoo"].calls[-1]["issues"]] == [70, 71]


def test_reconcile_pre_feature_flat_issues_no_parent_backfill(ctx_and_fakes):
    """DB wiped, pre-feature ticket: child marker finds flat issues, parent
    marker finds nothing -> do NOT synthesize a parent; all go to Odoo."""
    s = ctx_and_fakes
    s["github"].existing_issues = [
        {"number": 80, "title": "[Ticket 123] 1/2 — A", "id": 9080,
         "url": "https://github.com/acme/widgets/issues/80", "state": "open"},
        {"number": 81, "title": "[Ticket 123] 2/2 — B", "id": 9081,
         "url": "https://github.com/acme/widgets/issues/81", "state": "open"},
    ]
    s["github"].existing_parent = []
    params = _make_params(s["db"])
    out = run_ticket_issues(params)
    assert out["status"] == "completed"
    assert s["github"].created == []
    assert s["github"].sub_issues == []
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["parent_issue"] is None
    assert [i["number"] for i in s["odoo"].calls[-1]["issues"]] == [80, 81]


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
        run_id=0, odoo_instance_id=1, ticket_id=123, model_name="helpdesk.ticket",
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
        {"number": 102, "title": "[DEV] 123 - Issue 1 (1/2)",
         "url": "https://github.com/acme/widgets/issues/102", "state": "closed",
         "plan_date": None, "complete_date": None},
        {"number": 103, "title": "[DEV] 123 - Issue 2 (2/2)",
         "url": "https://github.com/acme/widgets/issues/103", "state": "open",
         "plan_date": None, "complete_date": None},
    ]


def test_all_issues_closed_notifies_odoo_ticket_ready(ctx_and_fakes):
    from worker.ticket_issue_runner import sync_ticket_issue_state

    s = ctx_and_fakes
    _seed_completed_run(s)
    s["odoo"].calls.clear()

    sync_ticket_issue_state(
        {"owner": "acme", "repo": "widgets", "number": 102, "state": "closed"}
    )
    out = sync_ticket_issue_state(
        {"owner": "acme", "repo": "widgets", "number": 103, "state": "closed"}
    )

    assert out == {"status": "completed", "records": 1, "notified": 1}
    assert s["odoo"].calls[-1]["ready"] is True
    assert [issue["state"] for issue in s["odoo"].calls[-1]["issues"]] == [
        "closed",
        "closed",
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


def test_created_issues_carry_plan_date(ctx_and_fakes):
    from datetime import date
    s = ctx_and_fakes
    params = _make_params(s["db"], plan_date=date(2026, 7, 15))
    run_ticket_issues(params)

    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert all(i["plan_date"] == "2026-07-15" for i in row["issues"])
    cb = s["odoo"].calls[0]
    assert all(i["plan_date"] == "2026-07-15" for i in cb["issues"])
    assert all(i["complete_date"] is None for i in cb["issues"])


def test_issue_closed_snapshot_carries_complete_date(ctx_and_fakes):
    from worker.ticket_issue_runner import sync_ticket_issue_state

    s = ctx_and_fakes
    _seed_completed_run(s)
    s["odoo"].calls.clear()

    sync_ticket_issue_state({"owner": "acme", "repo": "widgets", "number": 102,
                             "state": "closed", "closed_at": "2026-07-09T14:03:22Z"})
    snap = {i["number"]: i for i in s["odoo"].calls[0]["issues"]}
    assert snap[102]["complete_date"] == "2026-07-09"
    assert snap[103]["complete_date"] is None

    s["odoo"].calls.clear()
    sync_ticket_issue_state({"owner": "acme", "repo": "widgets", "number": 102,
                             "state": "open", "closed_at": None})
    snap = {i["number"]: i for i in s["odoo"].calls[0]["issues"]}
    assert snap[102]["complete_date"] is None


def test_sync_without_closed_at_key_still_works(ctx_and_fakes):
    from worker.ticket_issue_runner import sync_ticket_issue_state

    s = ctx_and_fakes
    _seed_completed_run(s)
    # Legacy deploy-window job params dict without the closed_at key.
    out = sync_ticket_issue_state({"owner": "acme", "repo": "widgets",
                                   "number": 102, "state": "closed"})
    assert out["status"] == "completed"


# --- Projects v2 board projection (spec 2026-07-09) ---------------------------


_PROJECT_URL = "https://github.com/orgs/acme/projects/5"


def _ops_events(db):
    from reva.db.models import OpsEvent
    with db.session() as s:
        return [(e.component, e.event, dict(e.detail or {})) for e in s.query(OpsEvent).all()]


def test_project_step_adds_all_items_and_sets_fields(ctx_and_fakes):
    from datetime import date
    s = ctx_and_fakes
    params = _make_params(s["db"], github_project_url=_PROJECT_URL,
                          plan_date=date(2026, 7, 15))
    out = run_ticket_issues(params)
    assert out["status"] == "completed"

    g = s["github"]
    # parent + 2 children added (default fixture lacks date+priority → created)
    assert len(g.project_items) == 3
    assert [f["name"] for f in g.created_fields] == ["Plan date", "Priority"]
    assert g.created_fields[0]["dataType"] == "DATE"
    assert g.created_fields[1]["dataType"] == "SINGLE_SELECT"
    # per added item: Plan date, Status=Todo, Priority (priority "1" → Medium)
    per_item = {}
    for item_id, field_id, value in g.item_field_sets:
        per_item.setdefault(item_id, []).append((field_id, value))
    assert len(per_item) == 3
    for sets in per_item.values():
        assert ("F_Plan date", "2026-07-15") in sets
        assert ("F_status", "opt_todo") in sets
        assert ("F_Priority", "opt_medium") in sets

    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["parent_issue"]["project_item_id"]
    assert all(i["project_item_id"] for i in row["issues"])
    # Odoo payload never carries the internal projection keys
    for issue in s["odoo"].calls[0]["issues"]:
        assert "node_id" not in issue
        assert "project_item_id" not in issue


def test_project_step_reuses_existing_plan_date_field(ctx_and_fakes):
    """A custom 'Plan date' DATE field is reused; the built-in issue-backed
    'Target date' is deliberately NOT matched (it rejects the standard
    mutation), so no field is created when our own 'Plan date' exists."""
    from datetime import date
    s = ctx_and_fakes
    s["github"].project_fields = s["github"].project_fields + [
        {"id": "F_target", "name": "Target date", "dataType": "DATE"},   # built-in, ignored
        {"id": "F_plan", "name": "Plan date", "dataType": "DATE"},        # our custom field
        {"id": "F_prio", "name": "Priority", "dataType": "SINGLE_SELECT",
         "options": [{"id": "opt_low", "name": "Low"},
                     {"id": "opt_medium", "name": "Medium"},
                     {"id": "opt_high", "name": "High"},
                     {"id": "opt_urgent", "name": "Urgent"}]},
    ]
    params = _make_params(s["db"], github_project_url=_PROJECT_URL,
                          plan_date=date(2026, 7, 15))
    run_ticket_issues(params)

    assert s["github"].created_fields == []
    sets = s["github"].item_field_sets
    assert ("PVTI_I_102", "F_plan", "2026-07-15") in sets           # our field set
    assert not any(f == "F_target" for _, f, _ in sets)             # built-in never touched


def test_project_step_creates_plan_date_when_only_builtin_target_date(ctx_and_fakes):
    """A board with only the built-in 'Target date' → REVA creates its own
    'Plan date' custom field rather than targeting the issue-backed built-in."""
    from datetime import date
    s = ctx_and_fakes
    s["github"].project_fields = s["github"].project_fields + [
        {"id": "F_target", "name": "Target date", "dataType": "DATE"},
    ]
    params = _make_params(s["db"], github_project_url=_PROJECT_URL,
                          plan_date=date(2026, 7, 15))
    run_ticket_issues(params)

    assert "Plan date" in [f["name"] for f in s["github"].created_fields]


def test_project_field_set_failure_is_isolated(ctx_and_fakes):
    """A single field-set failure must not sink item membership or the other
    fields: the item is still added, project_item_id persisted, and an ops
    event records the field error."""
    from datetime import date
    s = ctx_and_fakes
    s["github"].set_date_exc = PermanentError(
        "Issue field values cannot be updated using the updateProjectV2ItemFieldValue mutation")
    params = _make_params(s["db"], github_project_url=_PROJECT_URL,
                          plan_date=date(2026, 7, 15))
    out = run_ticket_issues(params)

    assert out["status"] == "completed"
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert all(i.get("project_item_id") for i in row["issues"])       # membership survived
    events = _ops_events(s["db"])
    assert ("github", "project_field_set_failed") in [(c, e) for c, e, _ in events]


@pytest.mark.parametrize("exc", [PermanentError("no permission"),
                                 TransientError("rate limited")])
def test_project_failure_is_fail_soft(ctx_and_fakes, exc):
    from datetime import date
    s = ctx_and_fakes
    s["github"].project_exc = exc
    params = _make_params(s["db"], github_project_url=_PROJECT_URL,
                          plan_date=date(2026, 7, 15))
    out = run_ticket_issues(params)

    assert out["status"] == "completed"
    assert s["odoo"].calls[0]["status"] == "created"     # callback unchanged
    events = _ops_events(s["db"])
    assert ("github", "project_step_failed") in [(c, e) for c, e, _ in events]


def test_project_step_skips_already_projected_items(ctx_and_fakes):
    from datetime import date
    s = ctx_and_fakes
    params = _make_params(s["db"], github_project_url=_PROJECT_URL,
                          plan_date=date(2026, 7, 15))
    run_ticket_issues(params)
    adds_before = len(s["github"].project_items)
    sets_before = len(s["github"].item_field_sets)

    # requeue: everything created+attached+projected → full short-circuit
    run_ticket_issues(params)
    assert len(s["github"].project_items) == adds_before
    assert len(s["github"].item_field_sets) == sets_before


def test_project_backfill_fetches_node_ids(ctx_and_fakes):
    s = ctx_and_fakes
    # A prior fully-created run WITHOUT node_ids (pre-feature), same basis.
    prior = _make_params(s["db"])
    parent = {"number": 50, "id": 900050, "url": "https://github.com/acme/widgets/issues/50",
              "title": "[DEV] 123 - Epic", "state": "open"}
    issues = [
        {"title": "[DEV] 123 - Issue 1 (1/2)", "type": "DEV", "number": 51, "id": 900051,
         "url": "https://github.com/acme/widgets/issues/51", "state": "open", "attached": True},
        {"title": "[DEV] 123 - Issue 2 (2/2)", "type": "DEV", "number": 52, "id": 900052,
         "url": "https://github.com/acme/widgets/issues/52", "state": "open", "attached": True},
    ]
    writers.set_ticket_issue_parent(s["db"], prior["run_id"], parent)
    writers.update_ticket_issue_progress(s["db"], prior["run_id"], issues)
    writers.record_ticket_issue_run_completed(s["db"], prior["run_id"], issues)

    # New request with a project URL adopts the plan and backfills the board.
    s["github"].issue_nodes = {50: "N50", 51: "N51"}   # 52: get_issue → None → skip
    params = _make_params(s["db"], github_project_url=_PROJECT_URL)
    out = run_ticket_issues(params)

    assert out["status"] == "completed"
    assert s["github"].created == []                   # nothing re-created
    assert s["github"].project_items == ["N50", "N51"]
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["parent_issue"]["project_item_id"] == "PVTI_N50"
    by_number = {i["number"]: i for i in row["issues"]}
    assert by_number[51]["project_item_id"] == "PVTI_N51"
    assert not by_number[52].get("project_item_id")    # heals on a later click


def test_no_project_url_no_project_calls(ctx_and_fakes):
    s = ctx_and_fakes
    run_ticket_issues(_make_params(s["db"]))
    assert s["github"].get_project_calls == 0
    assert s["github"].project_items == []
    assert s["github"].item_field_sets == []


def test_unmatched_todo_option_skips_status(ctx_and_fakes):
    from datetime import date
    s = ctx_and_fakes
    s["github"].project_fields = [
        {"id": "F_status", "name": "Status", "dataType": "SINGLE_SELECT",
         "options": [{"id": "opt_backlog", "name": "Backlog"},
                     {"id": "opt_done", "name": "Done"}]},
    ]
    params = _make_params(s["db"], github_project_url=_PROJECT_URL,
                          plan_date=date(2026, 7, 15))
    out = run_ticket_issues(params)

    assert out["status"] == "completed"
    assert len(s["github"].project_items) == 3          # items still added
    set_values = {v for _, _, v in s["github"].item_field_sets}
    assert "2026-07-15" in set_values                    # date set
    assert "opt_medium" in set_values                    # priority set
    assert not any(v.startswith("opt_") and v not in ("opt_medium",)
                   for v in set_values)                  # no status option set
    events = _ops_events(s["db"])
    unmatched = [(c, e, d) for c, e, d in events if e == "project_field_unmatched"]
    assert len(unmatched) == 1
    assert unmatched[0][2]["field"] == "Status"


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


def test_reconcile_splits_parent_from_children(ctx_and_fakes):
    """DB has no plan (e.g. wiped) but the marked issues still exist on GitHub:
    the marker search returns parent + children; the parent must be removed from
    the set sent to Odoo and recorded as parent_issue."""
    s = ctx_and_fakes
    s["github"].existing_issues = [
        {"number": 50, "title": "[Ticket 123] Login page broken", "id": 9050,
         "url": "https://github.com/acme/widgets/issues/50", "state": "open"},   # parent
        {"number": 51, "title": "[Ticket 123] 1/2 — A", "id": 9051,
         "url": "https://github.com/acme/widgets/issues/51", "state": "open"},
        {"number": 52, "title": "[Ticket 123] 2/2 — B", "id": 9052,
         "url": "https://github.com/acme/widgets/issues/52", "state": "open"},
    ]
    s["github"].existing_parent = [s["github"].existing_issues[0]]   # parent-marker hit
    params = _make_params(s["db"])

    out = run_ticket_issues(params)
    assert out["status"] == "completed"
    assert s["planner"].call_count == 0          # reconciled, not re-planned
    assert s["github"].created == []             # nothing created

    cb = s["odoo"].calls[-1]
    assert [i["number"] for i in cb["issues"]] == [51, 52]   # parent (50) excluded
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["parent_issue"]["number"] == 50
    # children get re-attached (idempotent)
    assert sorted(s["github"].sub_issues) == [(50, 9051), (50, 9052)]


def test_reconcile_single_issue_has_no_parent(ctx_and_fakes):
    s = ctx_and_fakes
    s["github"].existing_issues = [
        {"number": 60, "title": "[Ticket 123] only", "id": 9060,
         "url": "https://github.com/acme/widgets/issues/60", "state": "open"},
    ]
    s["github"].existing_parent = []
    params = _make_params(s["db"])

    out = run_ticket_issues(params)
    assert out["status"] == "completed"
    assert [i["number"] for i in s["odoo"].calls[-1]["issues"]] == [60]
    assert writers.get_ticket_issue_run(s["db"], params["run_id"])["parent_issue"] is None
    assert s["github"].sub_issues == []          # single issue, nothing attached


def test_instance_budget_gate_declines_planning(ctx_and_fakes, monkeypatch):
    """Over-budget instance: failed run + callback, no paid planning call."""
    s = ctx_and_fakes
    monkeypatch.setattr(
        "worker.ticket_issue_runner.instance_budget_exceeded", lambda ctx, iid: 12.5
    )
    params = _make_params(s["db"])

    with pytest.raises(PermanentError):
        run_ticket_issues(params)

    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["status"] == "failed"
    assert "budget" in row["error_message"].lower()
    assert s["planner"].call_count == 0
    assert s["odoo"].calls
    assert s["odoo"].calls[0]["status"] == "failed"
