# Ticket issues — GitHub Projects, plan date, complete date — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Odoo's `create-issues` request carries a `github_project_url` (+ optional `plan_date`), every issue REVA creates — children **and** the parent epic — is added to that GitHub Project with the plan date, Status=`Todo`, and Priority set as project fields. Every issue ref sent back to Odoo carries per-issue `plan_date` (creation-time echo) and `complete_date` (`YYYY-MM-DD` from GitHub's `closed_at`; cleared on reopen) — the shipped Odoo addon (`cu_reva_ticket_analysis` 19.0.11.2.0) already consumes exactly these keys. Without the new fields, behavior is byte-identical to today.

**Architecture:** Inbound contract gains two optional fields that flow `CreateIssuesRequest → TicketIssueJobParams → ticket_issue_runs` (two new columns). The worker gets a new **fail-soft** projection step after sub-issue attach: resolve the project via GraphQL (Projects v2 is GraphQL-only), ensure/locate the Plan date + Priority fields, `addProjectV2ItemById` each item (idempotent by API contract), set fields, persist `project_item_id` per item as the don't-touch-again guard. The per-issue dates ride the existing snapshot machinery: the creation loop stamps `plan_date` on each item, the state-sync writer stamps/clears `complete_date` (webhook `issue.closed_at` → job params → `update_ticket_issue_state`), and the union/ref projections carry both to every callback. Board visibility rides existing rails (ops events → TUI Failures tab) plus two new summary fields on the Tickets tab.

**Tech Stack:** Python 3.14, SQLAlchemy (SQLite tests / Postgres prod), `httpx` + MockTransport, RQ, pytest; Go/Bubble Tea TUI. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-09-github-projects-plan-date-design.md`.

## Global Constraints

- **Fail-soft everywhere for Projects** (spec decision 5): a Projects failure must never fail the run, block the `issues-created` callback, or change what Odoo receives. Every degradation logs AND `writers.record_ops_event(...)` (CLAUDE.md degradation invariant).
- **Odoo issue refs are extended by exactly two keys**: `_ISSUE_KEYS` in `reva/odoo_client.py` becomes `("number", "title", "url", "state", "plan_date", "complete_date")` (the shipped addon's `IssueItem`/`IssueStateItem` accept both, `.get()`-read, Date-truncating). `node_id`/`project_item_id` must never leak to Odoo — the `_project_items` projection guarantees this; assert it in tests. Dates on the wire are plain `YYYY-MM-DD` strings.
- **Never mutate existing project fields/options** — unmatched options are skipped with an ops event.
- **The persisted `project_item_id` is the guard against resetting a developer-moved card** (spec §runner step 3). Fields are set only when an item has no persisted `project_item_id`.
- **Contract changes are additive-optional** on both directions; regenerate `contracts/` (`python -m reva.odoo_contracts generate`) in the same commit as any payload-model change, or the drift tests fail. ast-odoo re-sync is a separate-repo follow-up (branch from `dev` → PR to `dev`).
- **Migration number:** `034` (next free after `033_ticket_analyses_callback.sql`); idempotent `ADD COLUMN IF NOT EXISTS`; exercised only on real Postgres (`make test-integration` / staging boot) — state that honestly.
- **Definition of done (CLAUDE.md):** shared `reva/` is touched → `make test` (worker + api + scheduler) + `ruff check reva worker/worker api/app scheduler/scheduler`; `tui/` touched → `cd tui && go build ./... && go vet ./... && go test ./...`.

---

### Task 1: Project URL parser

**Files:**
- Modify: `reva/github_urls.py`
- Test: `worker/tests/test_github_urls.py`

**Interfaces:**
- Produces: `parse_github_project_url(url: str) -> tuple[str, str, int] | None` — `("orgs"|"users", owner, project_number)`.

- [x] **Step 1: Write the failing tests**

Add to `worker/tests/test_github_urls.py` (match the file's existing parametrize style):

```python
import pytest

from reva.github_urls import parse_github_project_url


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/orgs/acme/projects/5", ("orgs", "acme", 5)),
    ("https://github.com/users/jo/projects/12", ("users", "jo", 12)),
    ("  https://github.com/orgs/acme/projects/5/  ", ("orgs", "acme", 5)),
    ("https://github.com/orgs/acme/projects/5/views/3", ("orgs", "acme", 5)),
])
def test_parse_github_project_url_accepts(url, expected):
    assert parse_github_project_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://github.com/acme/widgets",                    # repo, not a project
    "https://github.com/orgs/acme/projects/",             # no number
    "https://github.com/orgs/acme/projects/abc",          # non-numeric
    "http://github.com/orgs/acme/projects/5",             # not https
    "https://gitlab.com/orgs/acme/projects/5",            # wrong host
    "https://github.com/orgs/acme/projects/5/settings",   # extra segment
    "",
])
def test_parse_github_project_url_rejects(url):
    assert parse_github_project_url(url) is None
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_github_urls.py -v`
Expected: FAIL — `parse_github_project_url` does not exist.

- [x] **Step 3: Implement** in `reva/github_urls.py` (below `parse_github_repo_url`, same doc tone):

```python
_PROJECT_URL_RE = re.compile(
    r"^https://github\.com/(orgs|users)/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)"
    r"/projects/(\d+)(?:/views/\d+)?/?$"
)


def parse_github_project_url(url: str) -> tuple[str, str, int] | None:
    """Return ("orgs"|"users", owner, number) for a Projects v2 URL, else None.

    Tolerates surrounding whitespace, a trailing slash, and a /views/{n} suffix
    (what you get copying the address bar from an open board view); rejects
    other hosts, schemes, and extra path segments. Shared by the api route
    (reject at accept time with 422) and the worker (resolve the board).
    """
    match = _PROJECT_URL_RE.match(url.strip())
    if match is None:
        return None
    return match.group(1), match.group(2), int(match.group(3))
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_github_urls.py -v` — PASS.

- [x] **Step 5: Commit**

```bash
git add reva/github_urls.py worker/tests/test_github_urls.py
git commit -m "feat(github): parse Projects v2 board URLs"
```

---

### Task 2: Persistence — `github_project_url` + `plan_date` columns

**Files:**
- Create: `db/migrations/034_ticket_issue_project.sql`
- Modify: `reva/db/models.py` (`TicketIssueRun`, after `ticket_url` ~508)
- Modify: `reva/db/writers.py` (`record_ticket_issue_run_created` ~1673, `get_ticket_issue_run` ~1734)
- Modify: `reva/types.py` (`TicketIssueJobParams` ~405)
- Test: `worker/tests/test_ticket_issue_writers.py`

**Interfaces:**
- Produces:
  - `TicketIssueJobParams.github_project_url: str | None = None`, `.plan_date: date | None = None`
  - `TicketIssueRun.github_project_url` (Text, nullable), `.plan_date` (Date, nullable)
  - `get_ticket_issue_run(...)` dict gains keys `"github_project_url"`, `"plan_date"`
- Per-item JSON keys `node_id` / `project_item_id` need **no** schema or purge change: `purge_old_ticket_issue_text` strips only `body`/`acceptance_criteria` (verified 2026-07-09), so the new keys survive retention.

- [ ] **Step 1: Write the failing test**

Add to `worker/tests/test_ticket_issue_writers.py` (reuse its `_db()` / `_params()` helpers; extend `_params()` with a `**overrides` passthrough if it doesn't have one):

```python
def test_project_fields_default_none_and_round_trip():
    db = _db()
    run_id = writers.record_ticket_issue_run_created(db, _params())
    row = writers.get_ticket_issue_run(db, run_id)
    assert row["github_project_url"] is None
    assert row["plan_date"] is None

    from datetime import date
    p = _params()
    p2 = p.model_copy(update={
        "github_project_url": "https://github.com/orgs/acme/projects/5",
        "plan_date": date(2026, 7, 15),
    })
    run_id = writers.record_ticket_issue_run_created(db, p2)
    row = writers.get_ticket_issue_run(db, run_id)
    assert row["github_project_url"] == "https://github.com/orgs/acme/projects/5"
    assert row["plan_date"] == date(2026, 7, 15)
```

- [ ] **Step 2: Run to verify it fails** — `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_writers.py -v`

- [ ] **Step 3a: `reva/types.py`** — add to `TicketIssueJobParams` after `github_username` (import `date` from `datetime`):

```python
    # Optional Projects v2 board every created issue (and the epic) is added
    # to, and the planned date set on it. Absent → no Projects interaction.
    github_project_url: str | None = None
    plan_date: date | None = None
```

- [ ] **Step 3b: Migration** `db/migrations/034_ticket_issue_project.sql`:

```sql
-- Optional GitHub Projects v2 board + planned date sent by Odoo with a
-- create-issues request (spec 2026-07-09). NULL → no Projects interaction.
-- Per-item projection state (node_id, project_item_id) lives inside the
-- existing issues/parent_issue JSON, not in columns.
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS github_project_url TEXT;
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS plan_date DATE;
```

- [ ] **Step 3c: ORM** — in `reva/db/models.py` `TicketIssueRun`, after `ticket_url` (import `Date` from sqlalchemy alongside the existing imports):

```python
    # Optional Projects v2 board + planned date (migration 034); NULL → no
    # Projects interaction for this run.
    github_project_url: Mapped[str | None] = mapped_column(Text)
    plan_date: Mapped[date | None] = mapped_column(Date)
```

(`from datetime import date` — check the models file's existing datetime imports and extend.)

- [ ] **Step 3d: Writers** — `record_ticket_issue_run_created`: add `github_project_url=params.github_project_url, plan_date=params.plan_date,` to the `TicketIssueRun(...)` constructor. `get_ticket_issue_run`: add both keys to the returned dict (next to `"ticket_url"`).

- [ ] **Step 4: Run to verify it passes**, then the full writer + type suites:
`cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_writers.py tests/ -q` — PASS.

- [ ] **Step 5: Commit**

```bash
git add db/migrations/034_ticket_issue_project.sql reva/db/models.py reva/db/writers.py reva/types.py worker/tests/test_ticket_issue_writers.py
git commit -m "feat(db): persist github_project_url + plan_date on ticket_issue_runs"
```

---

### Task 3: Inbound contract — `create-issues` gains the two fields

**Files:**
- Modify: `api/app/schemas/ticket_issues.py` (`CreateIssuesRequest`)
- Modify: `api/app/routes/v1/ticket_issues.py` (`submit_create_issues` ~104, `requeue_ticket_issue_run` params ~273)
- Modify: `reva/odoo_contracts.py` (`create-issues` sample ~264)
- Regenerate: `contracts/`
- Test: `api/tests/test_v1_ticket_issues.py`, `api/tests/test_contracts_inbound.py` (drift only)

**Interfaces:**
- Produces: `CreateIssuesRequest.github_project_url: str | None = None`, `.plan_date: date | None = None`. Both flow into `TicketIssueJobParams` automatically — the route builds params via `**body.model_dump()` (no per-field wiring).
- Consumes: `parse_github_project_url` (Task 1), Task 2 columns (route persists via `record_ticket_issue_run_created`; requeue re-reads them).

- [ ] **Step 1: Write the failing tests**

Add to `api/tests/test_v1_ticket_issues.py` (reuse the file's client/auth/payload fixtures — grep the existing `submit` happy-path test and extend its payload):

```python
def test_create_issues_accepts_project_url_and_plan_date(...):
    # payload + {"github_project_url": "https://github.com/orgs/acme/projects/5",
    #            "plan_date": "2026-07-15"}
    # → 202; the enqueued job params (fake rq queue capture) carry both values,
    #   and writers.get_ticket_issue_run(db, request_id) round-trips them.

def test_create_issues_empty_strings_are_none(...):
    # {"github_project_url": "", "plan_date": ""} → 202; job params carry None/None.

def test_create_issues_invalid_project_url_is_422(...):
    # {"github_project_url": "https://github.com/acme/widgets"} → 422 with a
    #   message naming github_project_url; nothing enqueued, no run row created.

def test_requeue_carries_project_fields(...):
    # seed a failed run whose row has both values (Task 2 writer) → POST requeue
    # → re-enqueued job params include github_project_url + plan_date.
```

- [ ] **Step 2: Run to verify they fail** — `cd api && .venv/bin/python -m pytest tests/test_v1_ticket_issues.py -v`

- [ ] **Step 3a: Schema** — in `CreateIssuesRequest` (import `date` from `datetime`):

```python
    github_project_url: str | None = Field(
        default=None,
        description="Optional Projects v2 board URL "
        "(https://github.com/orgs/{org}/projects/{n}); every created issue "
        "and the parent epic are added to it.",
    )
    plan_date: date | None = Field(
        default=None,
        description="Optional planned date (YYYY-MM-DD) set as the board's "
        "'Plan date' field on every added item.",
    )
```

Extend the existing empty-string validators (mirror `_empty_username_is_none`) to cover both new fields — `plan_date` needs `mode="before"` so `""` becomes `None` before date parsing.

- [ ] **Step 3b: Route validation** — in `submit_create_issues`, after the `github_url` check:

```python
    if body.github_project_url is not None and parse_github_project_url(body.github_project_url) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="github_project_url must be an https://github.com/orgs/{org}/projects/{n} URL",
        )
```

(Import `parse_github_project_url`. No reachability probe — a missing App permission is handled fail-soft by the worker, spec decision 5.)

- [ ] **Step 3c: Requeue passthrough** — in `requeue_ticket_issue_run`'s `TicketIssueJobParams(...)`, add `github_project_url=row["github_project_url"], plan_date=row["plan_date"],`.

- [ ] **Step 3d: Contract sample** — in `reva/odoo_contracts.py`, add to the `create-issues` sample dict:

```python
            "github_project_url": "https://github.com/orgs/acme/projects/5",
            "plan_date": "2026-07-15",
```

Regenerate: `worker/.venv/bin/python -m reva.odoo_contracts generate` (from the repo root; the inbound JSONSchema updates automatically from the FastAPI model).

- [ ] **Step 4: Verify** — `cd api && .venv/bin/python -m pytest tests/ -q` (includes the contract drift test) — PASS.

- [ ] **Step 5: Commit**

```bash
git add api/app/schemas/ticket_issues.py api/app/routes/v1/ticket_issues.py reva/odoo_contracts.py contracts/ api/tests/test_v1_ticket_issues.py
git commit -m "feat(api): accept github_project_url + plan_date on create-issues"
```

---

### Task 4: Per-issue `plan_date` / `complete_date` on every callback ref

The shipped Odoo addon stores these as `issue_plan_date`/`issue_complete_date`
per `reva.github.issue` and renders them as list columns — the dates are
**per-issue inside the snapshots**, not top-level fields.

**Files:**
- Modify: `api/app/routes/webhooks.py` (`_handle_issues` ~448)
- Modify: `worker/worker/ticket_issue_runner.py` (`sync_ticket_issue_state` ~373; child-creation loop `issues[idx] = {...}` ~608)
- Modify: `reva/db/writers.py` (`update_ticket_issue_state` ~2059, `get_ticket_issue_union` ~2115)
- Modify: `reva/odoo_contracts.py` (`IssueRefPayload` ~34, `_ISSUE_SAMPLE` ~123 + the samples embedding it)
- Modify: `reva/odoo_client.py` (`_ISSUE_KEYS` ~61)
- Regenerate: `contracts/`
- Test: `api/tests/test_webhooks.py`, `worker/tests/test_ticket_issue_runner.py`, `worker/tests/test_ticket_issue_writers.py`, `worker/tests/test_odoo_client.py`

**Interfaces:**
- Produces: `IssueRefPayload.plan_date: str | None = None`, `.complete_date: str | None = None` (`YYYY-MM-DD`); `_ISSUE_KEYS += ("plan_date", "complete_date")`; per-item JSON keys `plan_date`/`complete_date`; `update_ticket_issue_state(db, owner, repo, number, state, closed_at: str | None = None)`; sync job params gain optional key `"closed_at"`.
- `complete_date` = `closed_at[:10]` (UTC date) when closing, `None` when reopening. `plan_date` = the creating run's `params.plan_date`, stamped at creation; adopted items keep their originating run's value, reconciled-from-GitHub items have none. Old queued jobs without `closed_at` must keep working (`.get`, not a required param).

- [ ] **Step 1: Write the failing tests**

`api/tests/test_webhooks.py` — extend the existing issues-webhook tests:

```python
# closed event: payload["issue"]["closed_at"] = "2026-07-09T14:03:22Z"
#   → enqueue args include {"closed_at": "2026-07-09T14:03:22Z"}
# reopened event → enqueue args include {"closed_at": None}
```

`worker/tests/test_ticket_issue_writers.py`:

```python
# test_update_state_stamps_complete_date: seed a run with a created issue,
#   update_ticket_issue_state(..., "closed", closed_at="2026-07-09T14:03:22Z")
#   → item["complete_date"] == "2026-07-09"; then state "open", closed_at=None
#   → item["complete_date"] is None.
# test_union_carries_dates: seeded items with plan_date/complete_date →
#   get_ticket_issue_union items include both keys (None when absent).
```

`worker/tests/test_ticket_issue_runner.py`:

```python
# test_created_issues_carry_plan_date: params with plan_date=date(2026, 7, 15)
#   → every created item in the run row has plan_date == "2026-07-15" and the
#   issues_created callback items carry it (FakeOdoo captures the list).
# test_issue_closed_snapshot_carries_complete_date: sync job with state
#   "closed" + closed_at → the issue_state snapshot item for that number has
#   complete_date == "2026-07-09"; reopened afterwards → None.
# test_sync_without_closed_at_key_still_works: legacy job params dict WITHOUT
#   the key (deploy-window jobs) → no KeyError.
```

`worker/tests/test_odoo_client.py` — posted `issues-created`/`issue-state` items carry exactly `("number", "title", "url", "state", "plan_date", "complete_date")`; extra keys like `node_id`/`project_item_id` are stripped.

- [ ] **Step 2: Run to verify they fail** — api + worker suites.

- [ ] **Step 3a: Webhook** — in `_handle_issues`, add `"closed_at": issue.get("closed_at")` to the enqueued params dict.

- [ ] **Step 3b: Writers** — `update_ticket_issue_state` gains `closed_at: str | None = None`; where it stamps `item["state"] = state`, also stamp `item["complete_date"] = (closed_at or "")[:10] or None` when `state == "closed"` else `None`. `get_ticket_issue_union` adds `"plan_date": item.get("plan_date")` and `"complete_date": item.get("complete_date")` to its projected dict.

- [ ] **Step 3c: Worker** — `sync_ticket_issue_state`: `closed_at = job_params.get("closed_at")` (NOT inside the required-key `try`), passed to `writers.update_ticket_issue_state(...)`. Child-creation loop: add `"plan_date": params.plan_date.isoformat() if params.plan_date else None` to the created-item dict (requires Task 2's params field).

- [ ] **Step 3d: Contract + client** — `IssueRefPayload` gains both optional fields; `_ISSUE_KEYS` extended (the `_project_items` projection then carries them everywhere refs are sent). Update `_ISSUE_SAMPLE` with `"plan_date": "2026-07-15", "complete_date": None` and give the issue-state sample's closed item `"complete_date": "2026-07-09"`. Regenerate `contracts/`.

- [ ] **Step 4: Verify** — `cd api && .venv/bin/python -m pytest tests/ -q && cd ../worker && .venv/bin/python -m pytest tests/ -q` — PASS.

- [ ] **Step 5: Commit**

```bash
git add api/app/routes/webhooks.py worker/worker/ticket_issue_runner.py reva/db/writers.py reva/odoo_contracts.py reva/odoo_client.py contracts/ api/tests/test_webhooks.py worker/tests/ worker/tests/test_odoo_client.py
git commit -m "feat(odoo): per-issue plan_date + complete_date on callback refs"
```

---

### Task 5: GitHub client — node ids + Projects v2 GraphQL methods

**Files:**
- Modify: `reva/github_client.py` (`create_issue` ~518, `get_issue` ~256, `find_issues_with_marker` ~629; new Projects section after the sub-issue/label block)
- Test: `worker/tests/test_github_client.py`

**Interfaces (produces):**
- `create_issue(...)` / `find_issues_with_marker(...)` items / `get_issue(...)` additionally return `"node_id"` (all three REST responses already contain it).
- `get_project(token, owner_type, owner, number) -> {"id": str, "fields": list[dict]}` — fields as `{"id", "name", "dataType", "options"?: [{"id","name"}]}`; raises `PermanentError` when the project resolves to null (bad number, or the App installation lacks the org **Projects** permission).
- `create_project_field(token, project_id, name, data_type, options: list[dict] | None = None) -> dict` (same field dict shape).
- `add_issue_to_project(token, project_id, content_node_id) -> str` (project item id; idempotent — GitHub returns the existing item).
- `set_project_item_date(token, project_id, item_id, field_id, date_value: str) -> None`
- `set_project_item_option(token, project_id, item_id, field_id, option_id) -> None`

All GraphQL calls go through the existing `self._post(token, "/graphql", {...})` + `_graphql_data(response, action)` (M7: errors surface as Transient/Permanent, never silent success). The **runner** owns fail-soft, not the client.

- [ ] **Step 1: Write the failing tests**

Follow the existing GraphQL test pattern (`get_review_threads` / `resolve_review_thread` tests use `_make_client` + a handler switching on `req.url.path == "/graphql"`). Cover:

```python
# test_create_issue_returns_node_id / test_get_issue_returns_node_id /
#   test_find_issues_with_marker_returns_node_id — REST fixtures gain "node_id".
# test_get_project_resolves_org_project — handler asserts the query contains
#   'organization' and variables {"login": "acme", "number": 5}; returns a
#   projectV2 with id "P_1" and fields [Status single-select w/ options,
#   "Target date" DATE] → parsed field dicts as specified above.
# test_get_project_null_raises_permanent — {"organization": {"projectV2": None}}
#   → PermanentError mentioning permission/not-found.
# test_add_issue_to_project_returns_item_id — mutation vars carry projectId +
#   contentId; returns item id "PVTI_1".
# test_set_project_item_date_and_option — variables carry the right value shape
#   ({"date": "2026-07-15"} / {"singleSelectOptionId": "opt1"}).
# test_create_project_field_single_select — input carries dataType +
#   singleSelectOptions; returns the parsed field dict incl. option ids.
# test_project_graphql_error_maps — errors array with type RATE_LIMITED →
#   TransientError (already the _graphql_data contract; one smoke assert).
```

- [ ] **Step 2: Run to verify they fail** — `cd worker && .venv/bin/python -m pytest tests/test_github_client.py -v`

- [ ] **Step 3: Implement.** Sketch (adapt names/docstrings to file conventions; one `# --- GitHub Projects v2 (GraphQL-only) ---` section):

```python
    _PROJECT_FIELDS_FRAGMENT = """
          ... on ProjectV2FieldCommon { id name dataType }
          ... on ProjectV2SingleSelectField { id name dataType options { id name } }"""

    def get_project(self, token: str, owner_type: str, owner: str, number: int) -> dict:
        """Resolve a Projects v2 board URL to its node id + fields (first 50).

        Projects v2 has no REST API. A null projectV2 means the number is wrong
        OR the App installation lacks the org-level Projects permission — the
        caller (runner) degrades fail-soft either way."""
        entity = "organization" if owner_type == "orgs" else "user"
        query = f"""
        query($login: String!, $number: Int!) {{
          {entity}(login: $login) {{
            projectV2(number: $number) {{
              id
              fields(first: 50) {{ nodes {{ {self._PROJECT_FIELDS_FRAGMENT} }} }}
            }}
          }}
        }}"""
        response = self._post(token, "/graphql",
                              {"query": query, "variables": {"login": owner, "number": number}})
        data = _graphql_data(response, "get_project")
        project = (data.get(entity) or {}).get("projectV2")
        if project is None:
            raise PermanentError(
                f"project {owner_type}/{owner}/projects/{number} not found "
                "(or the GitHub App lacks the org Projects permission)")
        return {"id": project["id"],
                "fields": [f for f in project["fields"]["nodes"] if f]}
```

`add_issue_to_project`: mutation `addProjectV2ItemById(input: {projectId, contentId}) { item { id } }` → return the item id. `set_project_item_date` / `set_project_item_option`: `updateProjectV2ItemFieldValue(input: {projectId, itemId, fieldId, value: {date: $date}})` / `value: {singleSelectOptionId: $optionId}` — call `_graphql_data` on the response (M7). `create_project_field`: `createProjectV2Field(input: {projectId, dataType, name, singleSelectOptions})` returning the same field fragment (`$dataType: ProjectV2CustomFieldType!`, options as `[{name, color, description}]`).

REST additions: `"node_id": data["node_id"]` in `create_issue`'s return; `"node_id": item["node_id"]` in `find_issues_with_marker` items; `"node_id"` (and keep `title`/`body`) in `get_issue`'s return.

- [ ] **Step 4: Run to verify they pass** — full `tests/test_github_client.py` (pre-existing tests included; the `get_issue` consumers in comment-reply paths only read `title`/`body`, so the additive key is safe — `grep -rn "get_issue(" worker/ reva/` to confirm).

- [ ] **Step 5: Commit**

```bash
git add reva/github_client.py worker/tests/test_github_client.py
git commit -m "feat(github): Projects v2 GraphQL methods + node_id capture"
```

---

### Task 6: Worker — fail-soft projection step

**Files:**
- Modify: `worker/worker/ticket_issue_runner.py` (`_plan_and_create` ~453-630: short-circuit ~499-507, new step after the attach loop ~621-628; new helpers near the other module helpers)
- Test: `worker/tests/test_ticket_issue_runner.py` (extend `FakeGitHub`, new tests)

**Interfaces:**
- Consumes: Task 5 client methods (via `ctx.github`), Task 1 parser, Task 2 params/row fields, `writers.update_ticket_issue_progress` / `set_ticket_issue_parent` / `record_ops_event`.
- Produces: per-item keys `node_id` (from creation) and `project_item_id` (set after projection) inside `issues` items and the `parent_issue` dict. Odoo payloads unchanged (`_ISSUE_KEYS` projection strips both).

**Field policy constants** (module level, near `_TYPE_LABELS`):

```python
# Board field policy (spec table): reuse-by-name, create only what's missing,
# never rewrite existing options (destructive to customer boards).
_PLAN_DATE_LOOKUP = ("plan date", "target date")   # case-insensitive, DATE type
_PLAN_DATE_FIELD = "Plan date"
_PRIORITY_FIELD = "Priority"
_PRIORITY_BY_ODOO = {"0": "Low", "1": "Medium", "2": "High", "3": "Urgent"}
_PRIORITY_CREATE_OPTIONS = [
    {"name": "Low", "color": "GRAY", "description": ""},
    {"name": "Medium", "color": "BLUE", "description": ""},
    {"name": "High", "color": "YELLOW", "description": ""},
    {"name": "Urgent", "color": "RED", "description": ""},
]
_STATUS_TODO = "todo"
```

- [ ] **Step 1: Extend `FakeGitHub` and write the failing tests**

`FakeGitHub` additions (defaults keep every existing test passing untouched):

```python
    # --- Projects v2 fakes ---------------------------------------------------
    project_fields: list[dict] = field(default_factory=lambda: [
        {"id": "F_status", "name": "Status", "dataType": "SINGLE_SELECT",
         "options": [{"id": "opt_todo", "name": "Todo"},
                     {"id": "opt_done", "name": "Done"}]},
    ])
    project_exc: Exception | None = None          # raised by get_project
    project_items: list[str] = field(default_factory=list)      # content node_ids added
    item_field_sets: list[tuple] = field(default_factory=list)  # (item_id, field_id, value)
    created_fields: list[dict] = field(default_factory=list)
    issue_nodes: dict[int, str] = field(default_factory=dict)   # number → node_id (backfill)

    def get_project(self, token, owner_type, owner, number):
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
        self.item_field_sets.append((item_id, field_id, date_value))

    def set_project_item_option(self, token, project_id, item_id, field_id, option_id):
        self.item_field_sets.append((item_id, field_id, option_id))

    def get_issue(self, token, owner, repo, number):
        node = self.issue_nodes.get(number)
        return {"title": "t", "body": "b", "node_id": node} if node else None
```

Also make `FakeGitHub.create_issue` return `"node_id": f"I_{self.next_number}"`.

New tests (`_make_params` gains project/plan-date overrides):

```python
# test_project_step_adds_all_items_and_sets_fields:
#   params with github_project_url + plan_date="2026-07-15", 2-issue plan
#   → 3 node_ids in github.project_items (parent + 2 children);
#     per added item: Plan date, Status=opt_todo, Priority option set
#     (default fixture lacks date+priority fields → created_fields has
#      "Plan date" DATE and "Priority" SINGLE_SELECT);
#   → run row: parent_issue and every issue carry project_item_id;
#   → Odoo callback issues have NO node_id/project_item_id keys.
# test_project_step_reuses_existing_target_date_field:
#   project_fields += Target date DATE + Priority w/ matching options
#   → created_fields == [].
# test_project_failure_is_fail_soft:
#   project_exc = PermanentError("no permission") → run completes,
#   issues_created callback sent, ops_events row (source "github",
#   "project_step_failed"); same for TransientError (still completes).
# test_project_step_skips_already_projected_items:
#   pre-persist plan where all items+parent have project_item_id →
#   requeue → no add_issue_to_project calls, no field sets (the
#   moved-card guard).
# test_project_backfill_fetches_node_ids:
#   adopted prior fully-created plan WITHOUT node_id (legacy) + request
#   with project URL → get_issue consulted per number (issue_nodes fixture),
#   items added; an issue get_issue returns None for is skipped, run completes.
# test_no_project_url_no_project_calls:
#   default params → project_items == [], get_project never called
#   (add a call counter) — byte-identical legacy behavior.
# test_unmatched_todo_option_skips_status:
#   Status options renamed (no "Todo") → item added, date+priority set,
#   no status set, ops event "project_field_unmatched" recorded.
```

Also assert in the existing happy-path test that nothing project-related fires (guards accidental coupling).

- [ ] **Step 2: Run to verify they fail** — `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_runner.py -v`

- [ ] **Step 3a: Board-context helper**

```python
def _board_context(ctx, token: str, params: TicketIssueJobParams, log) -> dict | None:
    """Resolve the target board once per run: project id + the field/option ids
    the projection loop needs. Returns None (after an ops event) when the URL
    doesn't parse — the route validates, so this guards requeued legacy rows.
    Raises on GraphQL errors; the caller's fail-soft wrapper owns those."""
    parsed = parse_github_project_url(params.github_project_url)
    if parsed is None:
        writers.record_ops_event(ctx.db, "github", "warning", "project_url_invalid",
                                 {"run_id": params.run_id})
        return None
    owner_type, owner, number = parsed
    project = ctx.github.get_project(token, owner_type, owner, number)
    fields = project["fields"]

    def _find(names: tuple[str, ...], data_type: str) -> dict | None:
        for name in names:
            for f in fields:
                if f["name"].lower() == name and f["dataType"] == data_type:
                    return f
        return None

    def _option_id(fld: dict, option_name: str, purpose: str) -> str | None:
        for opt in fld.get("options") or []:
            if opt["name"].lower() == option_name.lower():
                return opt["id"]
        writers.record_ops_event(ctx.db, "github", "warning", "project_field_unmatched",
                                 {"run_id": params.run_id, "field": fld["name"],
                                  "wanted": option_name, "purpose": purpose})
        return None

    date_field = _find(_PLAN_DATE_LOOKUP, "DATE")
    if date_field is None and params.plan_date is not None:
        date_field = ctx.github.create_project_field(
            token, project["id"], _PLAN_DATE_FIELD, "DATE")

    status_field = _find(("status",), "SINGLE_SELECT")
    todo_id = _option_id(status_field, _STATUS_TODO, "status") if status_field else None

    priority_field = _find((_PRIORITY_FIELD.lower(),), "SINGLE_SELECT")
    if priority_field is None:
        priority_field = ctx.github.create_project_field(
            token, project["id"], _PRIORITY_FIELD, "SINGLE_SELECT",
            options=_PRIORITY_CREATE_OPTIONS)
    wanted = _PRIORITY_BY_ODOO.get(params.priority, "Medium")
    priority_id = _option_id(priority_field, wanted, "priority")

    return {
        "project_id": project["id"],
        "date_field_id": date_field["id"] if date_field else None,
        "status": (status_field["id"], todo_id) if todo_id else None,
        "priority": (priority_field["id"], priority_id) if priority_id else None,
    }
```

- [ ] **Step 3b: Projection step** (called from `_plan_and_create`; the whole step is the fail-soft unit, per-item persistence keeps partial progress):

```python
def _project_step(ctx, token, owner, repo, params, issues, parent, log) -> None:
    """Add the epic + children to the requested Projects v2 board and stamp
    Plan date / Status=Todo / Priority. Fail-soft by spec decision 5: the board
    is a bonus — any failure logs + ops-events and the run completes. The
    persisted project_item_id is the only guard against re-setting fields on a
    card a developer already moved, so it is written after each item."""
    try:
        board = _board_context(ctx, token, params, log)
        if board is None:
            return

        def _node_id(item: dict) -> str | None:
            if item.get("node_id"):
                return item["node_id"]
            if item.get("number") is None:
                return None
            fetched = ctx.github.get_issue(token, owner, repo, item["number"])
            return (fetched or {}).get("node_id")  # None: deleted → skip

        def _place(item: dict) -> str | None:
            node = _node_id(item)
            if node is None:
                return None
            item_id = ctx.github.add_issue_to_project(token, board["project_id"], node)
            if board["date_field_id"] and params.plan_date is not None:
                ctx.github.set_project_item_date(
                    token, board["project_id"], item_id, board["date_field_id"],
                    params.plan_date.isoformat())
            for pair in (board["status"], board["priority"]):
                if pair:
                    ctx.github.set_project_item_option(
                        token, board["project_id"], item_id, pair[0], pair[1])
            item["node_id"] = node
            return item_id

        if parent is not None and not parent.get("project_item_id"):
            item_id = _place(parent)
            if item_id:
                parent["project_item_id"] = item_id
                writers.set_ticket_issue_parent(ctx.db, params.run_id, parent)
                log.info("ticket_issue_projected", issue=parent.get("number"))
        for idx, item in enumerate(issues):
            if item.get("project_item_id"):
                continue
            item_id = _place(item)
            if item_id:
                issues[idx] = {**item, "project_item_id": item_id}
                writers.update_ticket_issue_progress(ctx.db, params.run_id, issues)
                log.info("ticket_issue_projected", issue=item["number"])
    except Exception:
        log.warning("ticket_issues_project_step_failed", exc_info=True)
        writers.record_ops_event(
            ctx.db, "github", "warning", "project_step_failed",
            {"run_id": params.run_id, "ticket_id": params.ticket_id,
             "project_url": params.github_project_url})
```

- [ ] **Step 3c: Wire into `_plan_and_create`.** After the attach loop (step "3) attach each child…"), before `return issues`:

```python
    # 4) board projection (fail-soft; spec 2026-07-09)
    if params.github_project_url:
        _project_step(ctx, token, owner, repo, params, issues, parent, log)
```

And make the early short-circuit projection-aware — extend the `done` computation:

```python
        if done and params.github_project_url:
            done = all(i.get("project_item_id") for i in issues) and (
                parent is None or parent.get("project_item_id"))
```

(A fail-soft miss therefore heals on the next requeue/re-click for the ticket: numbers exist so nothing is re-created; `addProjectV2ItemById` is idempotent. A DB-wipe reconcile loses `project_item_id`s and re-stamps `Todo` on re-add — accepted, same narrow window as the existing marker-search caveats.)

- [ ] **Step 4: Run to verify they pass** — full `cd worker && .venv/bin/python -m pytest tests/ -q` — PASS (existing tests untouched by the default fixtures).

- [ ] **Step 5: Commit**

```bash
git add worker/worker/ticket_issue_runner.py worker/tests/test_ticket_issue_runner.py
git commit -m "feat(worker): fail-soft Projects v2 board projection for ticket issues"
```

---

### Task 7: Surface on `/api/v1` + TUI

**Files:**
- Modify: `api/app/schemas/ticket_issues.py` (`TicketIssueRunSummary` ~75, `TicketIssueRunStatus` ~100)
- Modify: `api/app/queries/ticket_issues.py` (`list_ticket_issue_runs` — include both fields in each item dict)
- Modify: `tui/internal/api/types.go` (`TicketIssueRunSummary` ~196), `tui/internal/api/mock.go` (one mock run), `tui/internal/ui/tickets.go` (detail view)
- Test: `api/tests/test_v1_ticket_issues.py` (list/status include the fields), `tui/internal/ui/tickets_test.go`

- [ ] **Step 1: Python side.** Add `github_project_url: str | None = None` and `plan_date: date | None = None` to `TicketIssueRunSummary` **and** `TicketIssueRunStatus` (defaults so legacy dicts validate); include both in the `list_ticket_issue_runs` item dicts (`get_ticket_issue_run` already carries them after Task 2). Extend an existing list-endpoint test: a seeded run with both values surfaces them; a legacy run yields `null`s. Run `cd api && .venv/bin/python -m pytest tests/ -q`.

- [ ] **Step 2: Go side.** `types.go`: `GithubProjectURL *string \`json:"github_project_url"\`` and `PlanDate *string \`json:"plan_date"\`` on `TicketIssueRunSummary`. `tickets.go` detail view: one muted line when set, e.g. `📋 <project-url> · plan 2026-07-15` (match the existing detail-row styling helpers; truncate to width). `mock.go`: set both on one mock run so `--demo` shows it. Extend `tickets_test.go` with a render assertion.

- [ ] **Step 3: Verify** — `cd tui && go build ./... && go vet ./... && go test ./...` — PASS.

- [ ] **Step 4: Commit**

```bash
git add api/app/schemas/ticket_issues.py api/app/queries/ticket_issues.py api/tests/test_v1_ticket_issues.py tui/
git commit -m "feat(api,tui): surface project board + plan date on ticket-issue runs"
```

---

### Task 8: Operator docs — board setup + App permission

**Files:**
- Create: `docs/github-projects.md`
- Modify: `docs/github-issue-creation.md` (link the new page from its setup/flow section)

- [ ] **Step 1: Write `docs/github-projects.md`** covering (spec "Documentation" section — workflows 1, 3, 5 are config-only):
  - **App permission:** add org-level **Projects: Read & write** to the GitHub App; every installation must re-approve. Until then REVA's board writes fail soft — issues still flow, failures land in ops events / the TUI Failures tab (`project_step_failed`).
  - **Fields REVA manages:** reuses a `Plan date` (or `Target date`) DATE field, else creates `Plan date`; reuses/creates a `Priority` single-select (Low/Medium/High/Urgent ← Odoo 0–3); sets Status to `Todo` only when first adding an item; never rewrites existing options (mismatches are skipped + ops-evented).
  - **Recommended built-in workflows** (project settings, zero API): *item closed → Status: Done*, *linked PR merged → Status: Done*.
  - **Recommended views:** Roadmap with its date field set to **Plan date**; board/table grouped by **Parent issue** (one swimlane per Odoo ticket, epic shows sub-issue progress); slice by `Priority` or the type labels (`BUG`/`FEAT`/…).
  - **Odoo side:** the project URL + plan date arrive per create-issues request; a re-click on an already-issued ticket backfills its existing issues onto the board.

- [ ] **Step 2: Cross-link** from `docs/github-issue-creation.md` and sanity-check statements against the shipped behavior of Tasks 5–6 (docs are treated as possibly stale — write only what the code does).

- [ ] **Step 3: Commit**

```bash
git add docs/github-projects.md docs/github-issue-creation.md
git commit -m "docs: GitHub Projects board setup for ticket issues"
```

---

### Final verification (run before opening a PR / deploying)

- [ ] `make test` — worker + api + scheduler all green (shared `reva/` touched).
- [ ] `ruff check reva worker/worker api/app scheduler/scheduler`
- [ ] `cd tui && go build ./... && go vet ./... && go test ./...`
- [ ] `worker/.venv/bin/python -m reva.odoo_contracts generate --check` — contracts current.
- [ ] State honestly: migration `034` and the GraphQL calls are unit-tested against SQLite/MockTransport only — validate on `make test-integration` / first staging boot.
- [ ] **Staging (after adding the App's org Projects permission and re-approving the installation):** one real create-issues run against a test board — items appear, Plan date/Status/Todo/Priority stamped; close an issue → the record's issue list in Odoo shows its Completed Date; break the permission on purpose once to see the fail-soft ops event.
- [ ] **Follow-up (separate repo):** ast-odoo already implements the consumer side (`cu_reva_ticket_analysis` 19.0.11.2.0, on `dev`) — after this ships, run its `sync_contracts.sh`, bump the `CONTRACTS_VERSION` pin in `cu_reva_connector/tests/test_contracts.py`, and drop the "awaiting a contract regen" notes from its CLAUDE.md/README/testguide; branch from `dev` → PR to `dev`.

## Self-Review

**Spec coverage:**
- Optional `github_project_url`/`plan_date` inbound, additive, empty-string tolerant → Tasks 1–3. ✓
- Parent epic + children projected, plan date on all (locked decision 2) → Task 6. ✓
- Field policy: reuse Plan date/Target date, create Plan date; Status=Todo; Priority Low/Medium/High/Urgent from Odoo 0–3; never mutate existing options → Tasks 5–6 (`_board_context`). ✓
- Fail-soft + ops events (decision 5, CLAUDE.md invariant) → Task 6 wrapper; TUI Failures tab picks ops events up automatically. ✓
- `project_item_id` guard against resetting moved cards; idempotent add; backfill via re-click (`get_issue` node fetch); projection-aware short-circuit → Task 6. ✓
- Per-issue `plan_date` echo + `complete_date` (`closed_at[:10]`, cleared on reopen) on every callback ref, matching the shipped addon's `IssueItem`/`IssueStateItem`; legacy queued jobs safe → Task 4. ✓
- Contracts regenerated in the same commits; Odoo issue payload keys frozen (`_ISSUE_KEYS` untouched, asserted in Task 4/6 tests). ✓
- TUI surfacing (CLAUDE.md rule 5) → Task 7. Docs for config-only workflows 1/3/5 → Task 8. ✓
- Out of scope respected: no status sync GitHub→Odoo, no re-dating projected items, no option rewrites.

**Placeholder scan:** Task 3/4/7 test bodies are behavior specs referencing existing fixtures to reuse (grep-to-locate), not invented APIs; every new function/field name, signature, and JSON key is fully specified. No TBD/TODO in code steps.

**Type consistency:** `plan_date` is `datetime.date` end-to-end (schema → params → ORM `Date` → `isoformat()` at the GraphQL boundary; RQ job args round-trip it via pickle, and `model_validate` re-coerces ISO strings). `node_id` (str, GraphQL) vs `id` (int, REST sub-issues) stay distinct keys — sub-issue attach keeps using `id`. `_place` signature consistent between runner and `FakeGitHub`. `complete_date` on the wire is a plain `YYYY-MM-DD` string (`closed_at[:10]`); the addon truncates defensively too, so a stray timestamp cannot break its Date fields.
