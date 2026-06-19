# Ticket issues — parent issue + GitHub sub-issues — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Group an Odoo ticket's generated GitHub issues under one synthesized parent ("epic") issue, with each planned issue attached as a GitHub sub-issue — while the callbacks REVA sends to Odoo stay byte-for-byte unchanged.

**Architecture:** All changes are in `cu_reva`. The worker creates a parent issue (when the plan has ≥2 issues), creates the children as today, then attaches each child to the parent via GitHub's sub-issues REST API. The parent is stored in a new `ticket_issue_runs.parent_issue` column and is deliberately excluded from the Odoo payload. A second, ticket-specific hidden marker on the parent body lets the DB-wiped reconciliation path tell the parent apart from its children.

**Tech Stack:** Python 3.14, SQLAlchemy (SQLite in tests / Postgres in prod), `httpx` (+ `MockTransport` in tests), RQ, pytest; Go/Bubble Tea for the read-only TUI.

## Global Constraints

- **Odoo contract is frozen.** `_issues_payload` and the `issues_created` / `issue_state` callbacks must keep sending only `{number, title, url}` for the **child** issues. The parent must never appear in any Odoo payload. (Spec decision 1.)
- **Parent synthesized locally** — no Claude/planner/prompt change. (Spec decision 2.)
- **No parent when the plan has exactly 1 issue.** (Spec decision 3.)
- **No Odoo-side change of any kind** (no auto-done, no new callback). (Spec out-of-scope.)
- **Child dedup marker is frozen:** children keep `<!-- revaticket<digest> -->` exactly as shipped (changing it would orphan every existing ticket's issues). The parent gets that marker **plus** an additional `<!-- revaticketparent<digest> -->`.
- **Sub-issue API needs the child's database `id`, not its `number`.**
- **Definition of done (CLAUDE.md):** `worker` + `reva` are touched → run `worker`, `api`, **and** `scheduler` suites (`make test`) plus `ruff`. Touching `tui/` → `cd tui && go build ./... && go vet ./... && go test ./...`. The Postgres-only migration is exercised on first staging boot / `make test-integration`, not the SQLite unit suite — state that honestly.

---

### Task 1: GitHub client — capture issue `id`, add `add_sub_issue`, return `id` from marker search

**Files:**
- Modify: `reva/github_client.py` (`create_issue` ~432-449, `find_issues_with_marker` ~523-547, add `add_sub_issue`)
- Test: `worker/tests/test_github_client.py`

**Interfaces:**
- Produces:
  - `create_issue(...) -> {"number": int, "url": str, "id": int}`
  - `add_sub_issue(token: str, owner: str, repo: str, parent_number: int, sub_issue_id: int) -> None`
  - `find_issues_with_marker(...) -> list[{"number", "title", "url", "state", "id"}]`

- [ ] **Step 1: Write the failing tests**

Add to `worker/tests/test_github_client.py` (follow the existing `_make_client(handler, private_pem)` + `httpx.MockTransport` pattern):

```python
def test_create_issue_returns_id(rsa_key_pair):
    _, private_pem = rsa_key_pair

    def handler(req):
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "tok", "expires_at": "2099-01-01T00:00:00Z"})
        assert req.url.path == "/repos/acme/widgets/issues"
        return httpx.Response(201, json={
            "number": 42, "html_url": "https://github.com/acme/widgets/issues/42", "id": 9001,
        })

    client = _make_client(handler, private_pem)
    out = client.create_issue("tok", "acme", "widgets", title="t", body="b", labels=["reva-ticket"])
    assert out == {"number": 42, "url": "https://github.com/acme/widgets/issues/42", "id": 9001}


def test_add_sub_issue_posts_sub_issue_id(rsa_key_pair):
    _, private_pem = rsa_key_pair
    seen = {}

    def handler(req):
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "tok", "expires_at": "2099-01-01T00:00:00Z"})
        assert req.url.path == "/repos/acme/widgets/issues/10/sub_issues"
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": 1})

    client = _make_client(handler, private_pem)
    client.add_sub_issue("tok", "acme", "widgets", parent_number=10, sub_issue_id=9001)
    assert seen["body"] == {"sub_issue_id": 9001}


def test_add_sub_issue_swallows_422_already_attached(rsa_key_pair):
    _, private_pem = rsa_key_pair

    def handler(req):
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "tok", "expires_at": "2099-01-01T00:00:00Z"})
        return httpx.Response(422, json={"message": "Sub-issue already added to this issue"})

    client = _make_client(handler, private_pem)
    # must not raise — re-attach on resume is a no-op
    client.add_sub_issue("tok", "acme", "widgets", parent_number=10, sub_issue_id=9001)


def test_find_issues_with_marker_returns_id(rsa_key_pair):
    _, private_pem = rsa_key_pair

    def handler(req):
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "tok", "expires_at": "2099-01-01T00:00:00Z"})
        return httpx.Response(200, json={"items": [
            {"number": 7, "title": "Old", "html_url": "https://github.com/acme/widgets/issues/7",
             "state": "open", "id": 700},
        ]})

    client = _make_client(handler, private_pem)
    out = client.find_issues_with_marker("tok", "acme", "widgets", "revaticketabc")
    assert out == [{"number": 7, "title": "Old",
                    "url": "https://github.com/acme/widgets/issues/7", "state": "open", "id": 700}]
```

Ensure `import json` is present at the top of the test file (add it if missing).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_github_client.py -k "id or sub_issue or marker" -v`
Expected: FAIL — `create_issue` result lacks `id`; `add_sub_issue` attribute does not exist.

- [ ] **Step 3: Implement the changes in `reva/github_client.py`**

In `create_issue`, return the `id`:

```python
        response = self._post(token, f"/repos/{owner}/{repo}/issues", payload)
        data = response.json()
        return {"number": data["number"], "url": data["html_url"], "id": data["id"]}
```

Add `add_sub_issue` directly below `create_issue`:

```python
    def add_sub_issue(
        self, token: str, owner: str, repo: str, parent_number: int, sub_issue_id: int
    ) -> None:
        """Attach an existing issue as a sub-issue of `parent_number`.

        The GitHub sub-issues API keys on the child's database `id` (the value
        create_issue returns), NOT its number. A 422 "already added" is a no-op:
        a resume/requeue must be able to re-run this without erroring."""
        try:
            self._post(
                token,
                f"/repos/{owner}/{repo}/issues/{parent_number}/sub_issues",
                {"sub_issue_id": sub_issue_id},
            )
        except PermanentError:
            # 4xx → PermanentError (map_github_status); 422 means it is already a
            # sub-issue of this parent, which is exactly the resumed-attach case.
            pass
```

In `find_issues_with_marker`, add `id` to each returned dict:

```python
        return [
            {
                "number": item["number"],
                "title": item["title"],
                "url": item["html_url"],
                "state": item.get("state", "open"),
                "id": item["id"],
            }
            for item in response.json().get("items", [])
        ]
```

> Note on the 422 swallow: `_post` raises whatever `map_github_status` returns for a 4xx, which is `PermanentError`. Catching `PermanentError` here means a *different* 4xx (e.g. 404 parent gone) is also swallowed; that is acceptable — attachment is best-effort relative to the issues already existing, and the next resume re-tries. If you prefer to swallow only 422, inspect the response inside `add_sub_issue` instead of using `_post` (out of scope here; keep it simple).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_github_client.py -v`
Expected: PASS (all, including pre-existing tests).

- [ ] **Step 5: Commit**

```bash
git add reva/github_client.py worker/tests/test_github_client.py
git commit -m "feat(github): capture issue id + add_sub_issue for sub-issue linking"
```

---

### Task 2: Persistence — `parent_issue` column, writers, purge

**Files:**
- Create: `db/migrations/017_ticket_issue_parent.sql`
- Modify: `reva/db/models.py` (`TicketIssueRun`, after the `issues` column ~425)
- Modify: `reva/db/writers.py` (`get_ticket_issue_run` ~1184, add `set_ticket_issue_parent`)
- Test: `worker/tests/test_ticket_issue_writers.py` (new)

**Interfaces:**
- Produces:
  - `TicketIssueRun.parent_issue` JSON column (nullable)
  - `writers.set_ticket_issue_parent(db, run_id: int, parent: dict) -> None`
  - `writers.get_ticket_issue_run(...)` dict now includes key `"parent_issue"`
- Consumes (later tasks): `parent` dict shape `{"number", "id", "url", "title", "state"}`.

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_ticket_issue_writers.py`:

```python
"""Writer-level tests for the parent_issue column (real SQLite)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from reva.db import Base, Database, create_engine_from_url, writers
from reva.types import TicketIssueJobParams


def _db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _params() -> TicketIssueJobParams:
    return TicketIssueJobParams(
        run_id=0, ticket_id=123, model_name="helpdesk.ticket",
        github_url="https://github.com/acme/widgets", name="n", description="d",
        analysis_html="a", priority="1", ticket_url="https://odoo.example/web#id=123",
    )


def test_parent_issue_defaults_to_none_and_round_trips():
    db = _db()
    run_id = writers.record_ticket_issue_run_created(db, _params())
    assert writers.get_ticket_issue_run(db, run_id)["parent_issue"] is None

    parent = {"number": 10, "id": 900, "url": "https://github.com/acme/widgets/issues/10",
              "title": "[Ticket 123] n", "state": "open"}
    writers.set_ticket_issue_parent(db, run_id, parent)
    assert writers.get_ticket_issue_run(db, run_id)["parent_issue"] == parent


def test_purge_preserves_parent_issue():
    db = _db()
    run_id = writers.record_ticket_issue_run_created(db, _params())
    writers.set_ticket_issue_parent(db, run_id, {"number": 10, "id": 900,
        "url": "https://github.com/acme/widgets/issues/10", "title": "[Ticket 123] n", "state": "open"})
    writers.update_ticket_issue_progress(db, run_id, [
        {"title": "c", "number": 11, "url": "u", "state": "open", "id": 901,
         "attached": True, "body": "secret", "acceptance_criteria": ["x"]},
    ])
    # backdate so the purge cutoff catches it
    from reva.db.models import TicketIssueRun
    with db.session() as s:
        s.get(TicketIssueRun, run_id).created_at = datetime.now(timezone.utc) - timedelta(days=40)

    writers.purge_old_ticket_issue_text(db, older_than_days=30)

    row = writers.get_ticket_issue_run(db, run_id)
    assert row["parent_issue"]["number"] == 10          # parent untouched
    assert "body" not in row["issues"][0]               # child text scrubbed
    assert row["issues"][0]["attached"] is True          # resume metadata kept
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_writers.py -v`
Expected: FAIL — `set_ticket_issue_parent` missing / `parent_issue` key absent.

- [ ] **Step 3a: Add the ORM column** in `reva/db/models.py`, immediately after the `issues` column:

```python
    issues: Mapped[Any | None] = mapped_column(JSON)
    # The parent ("epic") issue grouping this ticket's sub-issues, or NULL for
    # legacy and single-issue runs: {number, id, url, title, state}. Excluded
    # from every Odoo payload by design (it lives only on GitHub).
    parent_issue: Mapped[Any | None] = mapped_column(JSON)
```

- [ ] **Step 3b: Create the migration** `db/migrations/017_ticket_issue_parent.sql`:

```sql
-- The parent ("epic") issue that groups a ticket's generated issues as GitHub
-- sub-issues. NULL for legacy and single-issue runs. JSON: {number, id, url,
-- title, state}. Deliberately never sent to Odoo (the callback contract is
-- frozen); it exists only to wire up GitHub sub-issue links.
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS parent_issue JSONB;
```

- [ ] **Step 3c: Add the writer** in `reva/db/writers.py` after `update_ticket_issue_progress` (~1292):

```python
def set_ticket_issue_parent(db: Database, run_id: int, parent: dict) -> None:
    """Persist the parent ("epic") issue for a run. Statement-level UPDATE for
    the same reason as update_ticket_issue_progress: avoid dragging the full
    row (ticket text) over the wire to set one column."""
    with db.session() as s:
        s.execute(
            update(TicketIssueRun)
            .where(TicketIssueRun.id == run_id)
            .values(parent_issue=dict(parent))
        )
```

- [ ] **Step 3d: Surface it in `get_ticket_issue_run`** — add one line in the returned dict (after `"issues": row.issues,`):

```python
            "issues": row.issues,
            "parent_issue": row.parent_issue,
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_writers.py -v`
Expected: PASS. (`purge_old_ticket_issue_text` needs no change — it never touches `parent_issue`, which carries no customer text; the test proves it.)

- [ ] **Step 5: Commit**

```bash
git add db/migrations/017_ticket_issue_parent.sql reva/db/models.py reva/db/writers.py worker/tests/test_ticket_issue_writers.py
git commit -m "feat(db): add ticket_issue_runs.parent_issue column + writer"
```

---

### Task 3: Worker — create parent, create children, attach sub-issues (happy path, single-issue, resume)

**Files:**
- Modify: `worker/worker/ticket_issue_runner.py` (`_plan_and_create` ~257-352, helpers ~52-118)
- Test: `worker/tests/test_ticket_issue_runner.py` (update `FakeGitHub`, update existing assertions, add new tests)

**Interfaces:**
- Consumes: `ctx.github.add_sub_issue(token, owner, repo, parent_number, sub_issue_id)`; `create_issue(...)["id"]`; `writers.set_ticket_issue_parent`; `get_ticket_issue_run(...)["parent_issue"]`.
- Produces (helpers other tasks/tests call): `_ticket_digest(...)`, `_parent_marker(...)`, `_parent_title(params)`, `_format_parent_body(params, marker, parent_marker)`.
- Child item shape stored in `issues` now: `{"title", "number", "id", "url", "state", "attached"}`. Parent shape: `{"number", "id", "url", "title", "state"}`.

- [ ] **Step 1: Update `FakeGitHub` and add the new tests**

In `worker/tests/test_ticket_issue_runner.py`, extend `FakeGitHub`:

```python
@dataclass
class FakeGitHub:
    existing_issues: list[dict] = field(default_factory=list)        # child-marker search hits
    existing_parent: list[dict] = field(default_factory=list)        # parent-marker search hits
    created: list[dict] = field(default_factory=list)
    sub_issues: list[tuple[int, int]] = field(default_factory=list)  # (parent_number, sub_issue_id)
    labels_ensured: list[str] = field(default_factory=list)
    installation_exc: Exception | None = None
    create_exc_on_call: int | None = None
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
```

Add new tests:

```python
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
```

Update the **existing** tests that assumed a flat 2-issue plan:

- `test_happy_path_creates_issues_and_calls_back`: change `assert len(s["github"].created) == 2` → `== 3`; the body/marker assertions should target a **child** create — use `s["github"].created[1]["body"]` (index 0 is now the parent) for the `"Body 1"` / `"- [ ] criterion 1"` checks; the `cb["issues"]` expected numbers become `[102, 103]` with titles `"[Ticket 123] 1/2 — Issue 1"` / `"2/2 — Issue 2"` and URLs `/issues/102`,`/issues/103`; `row["issues"]` numbers become `[102, 103]`.
- `test_partial_failure_persists_progress_then_requeue_resumes`: `create_exc_on_call = 2` now hits **child 1** (call 1 = parent). Adjust: set `create_exc_on_call = 3` to fail child 2, and assert child 1 persisted / child 2 null accordingly; final numbers `[102, 103]`.
- `test_reclick_adopts_prior_plan_and_creates_missing`: the prior plan items need `id`/`attached` to resume cleanly — add `"id": 900_055, "attached": True` to the created item (number 55) and `"id": None, "attached": False` to the uncreated one. Expected: parent created + 1 child created; `cb["issues"]` numbers `[55, 101]` (parent excluded). Adjust the `len(created)` assertion to `2` (parent + 1 child).
- `test_prior_plan_for_different_repo_is_not_adopted`: 2-issue fresh plan → `len(created) == 3`.
- `test_transient_callback_error_after_creation_reraises_for_rq_retry`: `len(s["github"].created) == 3`; nothing re-created on the retry.
- `_seed_completed_run` / `sync_ticket_issue_state` tests: children are now numbers `102, 103` (parent took `101`). Update the expected numbers and titles in `test_issue_closed_updates_db_and_notifies_odoo` (and the open/closed indices) to `102/103`.

> When updating, prefer reading each child by searching `s["github"].created` for the one whose title contains `"1/2"` rather than hard-indexing, to keep the tests robust. Hard indices are fine where the order is deterministic (parent first, then children in plan order).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_runner.py -v`
Expected: FAIL — `_parent_title` / `add_sub_issue` flow not implemented; new assertions unmet.

- [ ] **Step 3: Implement in `worker/worker/ticket_issue_runner.py`**

Refactor the marker helper into a shared digest and add the parent marker (replaces `_ticket_marker` ~52-71):

```python
def _ticket_digest(
    owner: str, repo: str, model_name: str, ticket_id: int, basis: str
) -> str:
    """16-hex content-addressed digest of a ticket + planning basis (see
    _ticket_marker for why each field is in the key)."""
    key = f"{owner.lower()}/{repo.lower()}\x00{model_name}\x00{ticket_id}\x00{basis}"
    return hashlib.sha1(  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
        key.encode(), usedforsecurity=False
    ).hexdigest()[:16]


def _ticket_marker(
    owner: str, repo: str, model_name: str, ticket_id: int, basis: str
) -> str:
    """Stable token in EVERY issue body for this ticket (children + parent).
    Frozen string: changing it orphans every existing ticket's issues."""
    return "revaticket" + _ticket_digest(owner, repo, model_name, ticket_id, basis)


def _parent_marker(
    owner: str, repo: str, model_name: str, ticket_id: int, basis: str
) -> str:
    """Additional token in the PARENT body only — same digest, distinct prefix.
    Lets reconciliation (DB-wiped) tell the parent apart from its children via a
    second, ticket-specific search."""
    return "revaticketparent" + _ticket_digest(owner, repo, model_name, ticket_id, basis)
```

Add the parent title + body helpers near `_issue_title` / `_format_issue_body`:

```python
def _parent_title(params: TicketIssueJobParams) -> str:
    """Parent ("epic") title: '[Task 2010] <ticket name>' — same id prefix as the
    children, without the n/total order marker."""
    label = params.model_name.rsplit(".", 1)[-1].capitalize()
    return f"[{label} {params.ticket_id}] {params.name}"


def _format_parent_body(params: TicketIssueJobParams, marker: str, parent_marker: str) -> str:
    """Synthesized locally (no Claude): a back-link + both hidden markers. GitHub
    renders the sub-issue checklist itself, so we don't list children here."""
    return "\n".join([
        "Tracking issue for the linked Odoo ticket. "
        "Its work items are attached below as sub-issues.",
        "",
        "---",
        f"**Odoo ticket:** [{params.name}]({params.ticket_url})",
        "",
        "<sub>🤖 Created by REVA from an Odoo ticket.</sub>",
        f"<!-- {marker} -->",
        f"<!-- {parent_marker} -->",
    ])
```

Rewrite the body of `_plan_and_create` from the `marker = ...` line (~294) to the end. Replace this:

```python
    marker = _ticket_marker(owner, repo, params.model_name, params.ticket_id, basis)
    installation_id = ctx.github.get_repo_installation_id(owner, repo)
    token = ctx.github.get_installation_token(installation_id)

    if issues is None:
        existing = ctx.github.find_issues_with_marker(token, owner, repo, marker)
        if existing:
            log.info("ticket_issues_reconciled", existing=len(existing))
            return [dict(issue) for issue in existing]

        response, plan = ctx.ticket_issue_planner.plan_with_response(params)
        ...
    ctx.github.ensure_label(...)
    for idx, item in enumerate(issues):
        ...
    return issues
```

with this (note: the early-return on adoption short-circuit at ~289-292 must also become parent-aware — see Step 3b):

```python
    marker = _ticket_marker(owner, repo, params.model_name, params.ticket_id, basis)
    parent_marker = _parent_marker(owner, repo, params.model_name, params.ticket_id, basis)
    installation_id = ctx.github.get_repo_installation_id(owner, repo)
    token = ctx.github.get_installation_token(installation_id)

    if issues is None:
        existing = ctx.github.find_issues_with_marker(token, owner, repo, marker)
        if existing:
            # DB-wiped reconcile: the marker matches parent + children. Split the
            # parent out (Task 4) so it never reaches the Odoo payload.
            parent_hits = ctx.github.find_issues_with_marker(token, owner, repo, parent_marker)
            parent_numbers = {h["number"] for h in parent_hits}
            children = [e for e in existing if e["number"] not in parent_numbers]
            if parent_hits and parent is None:
                h = parent_hits[0]
                parent = {"number": h["number"], "id": h["id"], "url": h["url"],
                          "title": h["title"], "state": h.get("state", "open")}
                writers.set_ticket_issue_parent(ctx.db, params.run_id, parent)
            issues = [dict(c) for c in children]
            writers.update_ticket_issue_progress(ctx.db, params.run_id, issues)
            log.info("ticket_issues_reconciled", existing=len(existing), children=len(children))
        else:
            response, plan = ctx.ticket_issue_planner.plan_with_response(params)
            issues = [
                {
                    "title": item.title,
                    "body": item.body,
                    "acceptance_criteria": item.acceptance_criteria,
                    "number": None,
                    "url": None,
                    "state": None,
                    "id": None,
                    "attached": False,
                }
                for item in plan.issues
            ]
            cost = writers.record_ticket_issue_plan(ctx.db, params.run_id, issues, response)
            writers.record_claude_spend(ctx.db, "ticket_issues", cost)

    need_parent = len(issues) >= 2

    ctx.github.ensure_label(
        token, owner, repo, _TICKET_ISSUE_LABEL,
        description="Issues created from Odoo tickets by REVA",
    )

    # 1) parent first, so children can be attached to it
    if need_parent and parent is None:
        created = ctx.github.create_issue(
            token, owner, repo,
            title=_parent_title(params),
            body=_format_parent_body(params, marker, parent_marker),
            labels=[_TICKET_ISSUE_LABEL],
        )
        parent = {"number": created["number"], "id": created["id"],
                  "url": created["url"], "title": _parent_title(params), "state": "open"}
        writers.set_ticket_issue_parent(ctx.db, params.run_id, parent)
        log.info("ticket_issue_parent_created", issue=created["number"])

    # 2) children (unchanged loop, now also storing id + attached)
    for idx, item in enumerate(issues):
        if item.get("number") is not None:
            continue
        title = _issue_title(params, idx + 1, len(issues), item["title"])
        created = ctx.github.create_issue(
            token, owner, repo,
            title=title,
            body=_format_issue_body(item, params, marker),
            labels=[_TICKET_ISSUE_LABEL],
        )
        issues[idx] = {
            "title": title,
            "number": created["number"],
            "id": created["id"],
            "url": created["url"],
            "state": "open",
            "attached": False,
        }
        writers.update_ticket_issue_progress(ctx.db, params.run_id, issues)
        log.info("ticket_issue_created", issue=created["number"], title=title)

    # 3) attach each child to the parent (idempotent; 422 swallowed in client)
    if need_parent and parent is not None:
        for idx, item in enumerate(issues):
            if item.get("attached") or item.get("id") is None:
                continue
            ctx.github.add_sub_issue(token, owner, repo, parent["number"], item["id"])
            issues[idx] = {**item, "attached": True}
            writers.update_ticket_issue_progress(ctx.db, params.run_id, issues)
            log.info("ticket_issue_attached", issue=item["number"], parent=parent["number"])

    return issues
```

- [ ] **Step 3b: Make the early short-circuit parent-aware.** Load `parent` alongside `issues` near the top of `_plan_and_create` (after `issues = (row or {}).get("issues") or None`):

```python
    issues = (row or {}).get("issues") or None
    parent = (row or {}).get("parent_issue") or None
```

Replace the existing short-circuit block (~289-292):

```python
    if issues is not None and all(i.get("number") is not None for i in issues):
        return issues
```

with:

```python
    if issues is not None:
        need_parent = len(issues) >= 2
        done = all(i.get("number") is not None for i in issues)
        if need_parent:
            done = done and parent is not None and all(i.get("attached") for i in issues)
        if done:
            # Nothing to create/attach (callback resend / fully-created adoption):
            # skip the GitHub round-trips entirely.
            return issues
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_runner.py -v`
Expected: PASS (new + updated tests).

- [ ] **Step 5: Commit**

```bash
git add worker/worker/ticket_issue_runner.py worker/tests/test_ticket_issue_runner.py
git commit -m "feat(worker): create parent epic + attach generated issues as sub-issues"
```

---

### Task 4: Worker — DB-wiped reconciliation splits the parent out

This behavior was implemented in Task 3's `_plan_and_create` rewrite. This task adds the dedicated reconciliation tests proving the parent never leaks into the Odoo payload and is re-linked.

**Files:**
- Test: `worker/tests/test_ticket_issue_runner.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run the tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_runner.py -k reconcile -v`
Expected: PASS (the logic shipped in Task 3). If the single-issue reconcile test fails because the attach loop ran, confirm `need_parent` is `len(issues) >= 2` and the `existing_parent` fake is empty.

- [ ] **Step 3: (no implementation — covered by Task 3)**

- [ ] **Step 4: Run the full worker suite**

Run: `cd worker && .venv/bin/python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add worker/tests/test_ticket_issue_runner.py
git commit -m "test(worker): reconciliation splits parent epic from child issues"
```

---

### Task 5: Surface the parent in `/api/v1` + TUI

Per CLAUDE.md rule #5, the parent link must be visible in the read-only TUI, not only in the DB.

**Files:**
- Modify: `api/app/schemas/ticket_issues.py` (`TicketIssueRunSummary` ~53-67)
- Modify: the query backing `list_ticket_issue_runs` — module imported as `q` in `api/app/routes/v1/ticket_issues.py:180` (grep `def list_ticket_issue_runs` under `reva/db/` / `api/app/`); include `parent_issue` in each returned dict, mirroring `issues`.
- Modify: `tui/internal/api/types.go` (`TicketIssueRunSummary` ~188-200)
- Modify: `tui/internal/ui/tickets.go` (`detailView`)
- Modify: `tui/internal/api/mock.go` (`TicketIssueRuns` ~524, give one mock run a `ParentIssue`)
- Test: `api/tests/test_ticket_issues_api.py` (or the existing file covering this route); `tui/internal/ui/tickets_test.go`

**Interfaces:**
- Consumes: `get_ticket_issue_run(...)["parent_issue"]` and the list query's `parent_issue`.
- Produces: `TicketIssueRunSummary.parent_issue: TicketIssueRef | None` (Python) / `ParentIssue *TicketIssueRef` (Go).

- [ ] **Step 1: Add the API field (with a default so legacy rows validate)**

In `api/app/schemas/ticket_issues.py`, add to `TicketIssueRunSummary`:

```python
    issues: list[TicketIssueRef]
    parent_issue: TicketIssueRef | None = None
```

Then ensure the dict the route validates carries it: in the query function backing `list_ticket_issue_runs` (the `q.list_ticket_issue_runs` call at `api/app/routes/v1/ticket_issues.py:180`), include `"parent_issue": row.parent_issue` in each returned dict next to `"issues"`. `model_validate` then populates it (and tolerates `None`).

- [ ] **Step 2: Write/extend the API test**

Add a test asserting a run with a parent surfaces it and a run without one yields `null`:

```python
def test_list_runs_includes_parent_issue(...):
    # seed one run via writers.set_ticket_issue_parent(...) then GET /api/v1/ticket-issue-runs
    # assert items[i]["parent_issue"]["number"] == <n>, and a parentless run has parent_issue is None
```

(Model after the existing list-endpoint test in the file — reuse its client/seed fixtures.)

Run: `cd api && .venv/bin/python -m pytest tests/ -k ticket_issue -v`
Expected: PASS.

- [ ] **Step 3: TUI type + render + mock**

In `tui/internal/api/types.go`, add to `TicketIssueRunSummary`:

```go
	Issues           []TicketIssueRef `json:"issues"`
	ParentIssue      *TicketIssueRef  `json:"parent_issue"`
```

In `tui/internal/ui/tickets.go` `detailView`, render the parent above the issue list when present, e.g.:

```go
if t.issueRun != nil && t.issueRun.ParentIssue != nil && t.issueRun.ParentIssue.Number != nil {
	p := t.issueRun.ParentIssue
	lines = append(lines, styleSubtitle.Render(
		fmt.Sprintf("  Epic: #%d %s", *p.Number, truncate(p.Title, w-12))))
}
```

(Adapt to `detailView`'s actual local variable for the assembled lines and styling helpers; keep it one muted line.)

In `tui/internal/api/mock.go` `TicketIssueRuns`, set `ParentIssue` on one of the mock runs so `--demo` shows it:

```go
ParentIssue: &TicketIssueRef{Number: intptr(900), Title: "[Task 42] Build the thing",
	URL: strptr("https://github.com/acme/widgets/issues/900"), State: strptr("open")},
```

(Use the file's existing pointer helpers; if none exist, add small `intptr`/`strptr` helpers or inline addresses of locals.)

- [ ] **Step 4: Build/vet/test the TUI**

Run: `cd tui && go build ./... && go vet ./... && go test ./...`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/app/schemas/ticket_issues.py api/app/routes/v1/ticket_issues.py reva/db/ api/tests tui/
git commit -m "feat(api,tui): surface the parent epic issue on create-issues runs"
```

---

### Final verification (run before opening a PR)

- [ ] `make test` (worker + api + scheduler all green — shared `reva/` was touched).
- [ ] `ruff check reva worker/worker api/app scheduler/scheduler`
- [ ] `cd tui && go build ./... && go vet ./... && go test ./...`
- [ ] State honestly that the migration `017` is **not** exercised by the SQLite unit suite — validate it via `make test-integration` or first staging boot, and confirm on the first real run that the App token can call `POST .../sub_issues` (the endpoint is newer than issue creation).

## Self-Review

**Spec coverage:**
- Parent created for ≥2 issues, synthesized locally → Task 3. ✓
- Skip parent for 1 issue → Task 3 (`need_parent`), tested. ✓
- Sub-issue attach by `id` → Tasks 1 + 3. ✓
- Parent excluded from Odoo payload → unchanged `_issues_payload`; asserted in Task 3/4. ✓
- `parent_issue` column + purge preserves it → Task 2. ✓
- Idempotent resume (attached flag) + 422 swallow → Tasks 1 + 3. ✓
- Reconciliation splits parent via `revaticketparent<digest>` → Tasks 3 + 4. ✓
- Label on parent / no auto-done → parent gets `reva-ticket`; no Odoo callback added (nothing in any task adds one). ✓
- TUI/API surfacing (rule #5) → Task 5. ✓

**Placeholder scan:** Task 5 Step 1/3 references "the query backing `list_ticket_issue_runs`" and the `detailView` local — these are grep-to-locate, not invented APIs; the field name (`parent_issue` / `ParentIssue`) and shape are fully specified. No TBD/TODO in code steps.

**Type consistency:** `create_issue` returns `id` (Task 1) consumed in Task 3; child item keys `{title, number, id, url, state, attached}` consistent across create/attach/short-circuit; parent shape `{number, id, url, title, state}` consistent across writer, runner, reconcile, schema; `add_sub_issue(token, owner, repo, parent_number, sub_issue_id)` signature identical in client, fake, and call site; markers `_ticket_marker` (frozen) vs `_parent_marker` share `_ticket_digest`.
