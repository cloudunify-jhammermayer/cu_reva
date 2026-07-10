# Issue Estimates on the Odoo Ticket — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-issue `estimate_hours` flows on every Odoo callback snapshot, plus a `total_estimate_hours` on the issues-created callback, so consultants see effort on the ticket without opening GitHub.

**Spec:** `docs/superpowers/specs/2026-07-10-issue-estimates-to-odoo-design.md` (approved 2026-07-10).

**Architecture:** The persisted issue refs in `ticket_issue_runs.issues` already carry `estimate_hours`; the data is currently dropped in two places — `get_ticket_issue_union` (rebuilds union items with a fixed key set) and `_ISSUE_KEYS` (filters callback snapshots). Add the key to both, add the contract fields (additive), compute the total at the created-callback call site, and surface per-issue estimates on the internal API + TUI tickets detail. **Spec correction discovered during planning:** the spec's Context says "the union snapshot builder passes them through" — it does NOT (fixed key set at `reva/db/writers.py:2162-2168`); Task 2 fixes that, and Task 5 corrects the spec text.

**Tech Stack:** Python 3.14 (pydantic contract models, SQLAlchemy), generated `contracts/` artifacts, Go/Bubble Tea TUI.

## Global Constraints

- **Additive-only contract change** (shipped-addon contract note allows optional additive fields): `IssueRefPayload.estimate_hours: float | None = None`, `IssuesCreatedPayload.total_estimate_hours: float | None = None`. No renames, no removals, no required fields.
- **Contracts regen is mandatory** after any model/sample change: `worker/.venv/bin/python -m reva.odoo_contracts generate` from the repo root; `worker/tests/test_contracts_drift.py` gates it.
- **Optional-key omission preserved:** `_project_items` sends `estimate_hours` only when present on the item — pre-rollout runs omit it per item; `total_estimate_hours` is `None` (never `0`) when no item carries an estimate.
- **Total = children only.** The parent epic lives in `parent_issue` (never in the union), so summing union items cannot double-count. Sum idiom: `round(sum(i.get("estimate_hours") or 0 for i in union), 2)`, sent as `total or None`.
- The ast-odoo consumer (rendering + "Dev estimate (low-end, AI-assisted)" labeling) is coordinated separate work in the ast-odoo repo — NOT in scope; the final report must state that Odoo won't display estimates until that addon update, and that sending them early is safe (addon `.get()`-reads and ignores unknown keys).
- `reva/` is shared by all three services: final verification is `make test`, `ruff check reva worker/worker api/app scheduler/scheduler`, `cd tui && go build ./... && go vet ./... && go test ./...`, `gofmt -l tui/` empty.
- Per-service venvs: `cd worker && .venv/bin/python -m pytest tests/...` (same for `api/`).

---

### Task 1: Contract models + callback client + contracts regen

**Files:**
- Modify: `reva/odoo_contracts.py` (`IssueRefPayload` ~line 34, `IssuesCreatedPayload` ~line 50, `_ISSUE_SAMPLE` ~line 128, the `tickets.issues-created` Contract sample/extra_samples ~lines 162-184)
- Modify: `reva/odoo_client.py` (`_ISSUE_KEYS` line 61, `issues_created` ~line 165)
- Modify: `worker/tests/test_odoo_client.py` (issues_created section, ~line 211)
- Regenerate: `contracts/` (committed artifacts)

**Interfaces:**
- Produces: `OdooCallbackClient.issues_created(..., total_estimate_hours: float | None = None)` — Task 2's runner passes it. Snapshot items on all three issue callbacks (`issues-created`, `issue-state`, `ready`) now pass `estimate_hours` through when present.

- [ ] **Step 1: Write the failing tests**

In `worker/tests/test_odoo_client.py`, append to the issues_created section:

```python
def test_issues_created_includes_estimates_and_total(monkeypatch):
    captured: dict = {}

    def post(url, *, json, **kwargs):
        captured["body"] = json
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    kwargs = _issues_kwargs()
    kwargs["issues"] = [
        {"number": 42, "title": "Implement login form",
         "url": "https://github.com/org/repo/issues/42", "estimate_hours": 2.5},
        {"number": 43, "title": "No estimate",
         "url": "https://github.com/org/repo/issues/43"},
    ]
    _client().issues_created(**kwargs, total_estimate_hours=2.5)
    items = captured["body"]["issues"]
    assert items[0]["estimate_hours"] == 2.5
    # Pre-rollout items simply omit the key (optional-key omission).
    assert "estimate_hours" not in items[1]
    assert captured["body"]["total_estimate_hours"] == 2.5
```

Also verify the shared `_ISSUE_KEYS` filter carries the estimate on the other
two snapshot callbacks (spec testing section: end-to-end through all three):

```python
def test_issue_state_and_ready_snapshots_carry_estimate(monkeypatch):
    captured: list[dict] = []

    def post(url, *, json, **kwargs):
        captured.append(json)
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    snapshot = [{"number": 42, "title": "t", "url": "https://gh/42",
                 "state": "closed", "estimate_hours": 3.0}]
    _client().issue_state(ticket_id=1, model_name="helpdesk.ticket",
                          number=42, state="closed", issues=snapshot)
    _client().tickets_ready(ticket_id=1, model_name="helpdesk.ticket",
                            issues=snapshot)
    assert all(body["issues"][0]["estimate_hours"] == 3.0 for body in captured)
```

And update the existing full-body equality test — `model_dump()` now emits the
new field's `None` default:

```python
def test_issues_created_posts_contract_payload_to_sibling_path(monkeypatch):
    captured: dict = {}

    def post(url, *, json, headers, **kwargs):
        captured["url"] = url
        captured["body"] = json
        captured["auth"] = headers.get("Authorization", "")
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    _client().issues_created(**_issues_kwargs())

    # base URL is derived from the configured callback URL — no new config
    assert captured["url"] == "https://odoo.example.com/api/reva/tickets/issues-created"
    assert captured["auth"] == f"Bearer {_KEY}"
    expected = {**_issues_kwargs(), "total_estimate_hours": None}
    assert captured["body"] == expected
```

- [ ] **Step 2: Run tests to verify the new one fails**

Run: `cd worker && .venv/bin/python -m pytest tests/test_odoo_client.py -k issues_created -v`
Expected: `test_issues_created_includes_estimates_and_total` FAILS (`TypeError: unexpected keyword argument 'total_estimate_hours'`); the updated equality test FAILS (body lacks the key).

- [ ] **Step 3: Implement the contract + client changes**

`reva/odoo_contracts.py` — in `IssueRefPayload`, after `complete_date`:

```python
    # Low-end AI-assisted dev estimate in hours (spec 2026-07-10). Omitted on
    # pre-rollout items; the Odoo addon .get()-reads it.
    estimate_hours: float | None = None
```

In `IssuesCreatedPayload`, after `issues`:

```python
    # Sum over union items carrying an estimate (children only — the epic is
    # parent_issue, never in the union). None when no item has an estimate.
    total_estimate_hours: float | None = None
```

Update `_ISSUE_SAMPLE` (the issue-state/ready samples spread it, so they
inherit the field):

```python
_ISSUE_SAMPLE = {
    "number": 42,
    "title": "Implement login form",
    "url": "https://github.com/acme/widgets/issues/42",
    "state": "open",
    "plan_date": "2026-07-15",
    "complete_date": None,
    "estimate_hours": 3.5,
}
```

In the `tickets.issues-created` Contract: add `"total_estimate_hours": 3.5,`
to `sample` (after `"issues": [_ISSUE_SAMPLE],`) and
`"total_estimate_hours": None,` to the failed `extra_samples` entry (after
`"issues": [],`).

`reva/odoo_client.py` line 61:

```python
_ISSUE_KEYS = (
    "number", "title", "url", "state", "plan_date", "complete_date",
    "estimate_hours",
)
```

`issues_created` — add the parameter and pass it into the payload:

```python
    def issues_created(
        self,
        ticket_id: int,
        model_name: str,
        request_id: int,
        status: str,
        issues: list[dict],
        error: str | None = None,
        total_estimate_hours: float | None = None,
    ) -> None:
```

and in the `IssuesCreatedPayload(...)` construction add
`total_estimate_hours=total_estimate_hours,` (the docstring's field list gains
one line; `body = payload.model_dump(exclude={"issues"})` picks it up).

- [ ] **Step 4: Run the client + contracts suites**

Run: `cd worker && .venv/bin/python -m pytest tests/test_odoo_client.py tests/test_odoo_contracts.py -v`
Expected: PASS. `tests/test_contracts_drift.py::test_committed_contracts_are_current` would now FAIL — regen next.

- [ ] **Step 5: Regenerate contracts and verify drift gate**

Run from the repo root:
```bash
worker/.venv/bin/python -m reva.odoo_contracts generate
cd worker && .venv/bin/python -m pytest tests/test_contracts_drift.py -v
```
Expected: generation rewrites files under `contracts/`; drift test PASSES.

- [ ] **Step 6: Commit**

```bash
git add reva/odoo_contracts.py reva/odoo_client.py worker/tests/test_odoo_client.py contracts/
git commit -m "feat(contracts): per-issue estimate_hours + total_estimate_hours on issue callbacks"
```

---

### Task 2: Union propagation + runner total

**Files:**
- Modify: `reva/db/writers.py` (`get_ticket_issue_union`, the `seen[n] = {...}` dict ~line 2162)
- Modify: `worker/worker/ticket_issue_runner.py` (created-path callback ~line 411)
- Test: `worker/tests/test_ticket_issue_writers.py`, `worker/tests/test_ticket_issue_runner.py`

**Interfaces:**
- Consumes: `issues_created(..., total_estimate_hours=)` from Task 1.
- Produces: union items now include `estimate_hours: float | None` — Task 3's API query and the issue-state/ready callbacks read them.

- [ ] **Step 1: Write the failing tests**

In `worker/tests/test_ticket_issue_writers.py`, after `test_union_carries_dates` (uses the file's existing `_complete_run`/`_typed_params` helpers):

```python
def test_union_carries_estimate_hours(db):
    _complete_run(db, _typed_params(ticket_id=96), [
        {"number": 40, "title": "A", "url": "https://gh/40", "state": "open",
         "estimate_hours": 2.5},
        {"number": 41, "title": "B (pre-rollout run)", "url": "https://gh/41",
         "state": "open"},
    ])
    union = writers.get_ticket_issue_union(db, 1, 96, "helpdesk.ticket")
    assert union[0]["estimate_hours"] == 2.5
    assert union[1]["estimate_hours"] is None
```

In `worker/tests/test_ticket_issue_runner.py`: extend the `FakeOdoo.issues_created` signature (line ~166) with `total_estimate_hours=None` and capture it the same way the other kwargs are captured, then add (using the file's existing fixtures — the default fixture plans issues with `estimate_hours=1.5`, see line ~201):

```python
def test_created_callback_carries_estimates_and_total(ctx_and_fakes):
    ctx, fakes = ctx_and_fakes
    run_ticket_issues(ctx, _params())
    call = fakes.odoo.issues_created_calls[-1]
    assert call["status"] == "created"
    assert all(i.get("estimate_hours") == 1.5 for i in call["issues"])
    assert call["total_estimate_hours"] == round(1.5 * len(call["issues"]), 2)
```

Adapt the capture/access idiom (`issues_created_calls`, `_params`, the run entrypoint name) to what the file actually uses — read its existing `issues_created` assertions first and mirror them exactly; the assertions above are the requirement.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_writers.py -k estimate -v tests/test_ticket_issue_runner.py -k estimates_and_total -v`
Expected: FAIL — union items have no `estimate_hours` key; FakeOdoo captures no total.

- [ ] **Step 3: Implement**

`reva/db/writers.py`, in `get_ticket_issue_union`'s `seen[n] = {...}` dict, after `"complete_date": item.get("complete_date"),`:

```python
                    "estimate_hours": item.get("estimate_hours"),
```

`worker/worker/ticket_issue_runner.py`, at the created-path callback (~line 411), compute the total from the union (same idiom as `_format_parent_body`) and pass it:

```python
    union = writers.get_ticket_issue_union(
        ctx.db, params.odoo_instance_id, params.ticket_id, params.model_name
    )
    total = round(sum(i.get("estimate_hours") or 0 for i in union), 2)
    try:
        odoo.issues_created(
            ticket_id=params.ticket_id,
            model_name=params.model_name,
            request_id=params.run_id,
            status="created",
            issues=union,
            total_estimate_hours=total or None,
        )
```

The failed-path callback (`_send_failed_callback`) stays untouched — it sends `issues=[]` and the parameter defaults to `None`.

- [ ] **Step 4: Run the covering suites**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_writers.py tests/test_ticket_issue_runner.py tests/test_ticket_links.py -q`
Expected: all PASS (union shape change must not break ready-signal/state-sync consumers — they read `number`/`state` only).

- [ ] **Step 5: Commit**

```bash
git add reva/db/writers.py worker/worker/ticket_issue_runner.py worker/tests/test_ticket_issue_writers.py worker/tests/test_ticket_issue_runner.py
git commit -m "feat(tickets): estimates flow through the issue union; created callback sends the total"
```

---

### Task 3: API — per-issue estimate on ticket-issue runs

**Files:**
- Modify: `api/app/queries/ticket_issues.py` (per-issue dict ~line 48; update the "stripped to {number, title, url}" docstring at ~line 19)
- Modify: `api/app/schemas/ticket_issues.py` (the response model for issue refs — locate the class the route's response_model uses for issue items; add the optional field)
- Test: `api/tests/test_v1_ticket_issues.py`

**Interfaces:**
- Consumes: union/stored items carrying `estimate_hours` (Task 2).
- Produces: `GET /api/v1/ticket-issues` items' `issues[]` entries gain `estimate_hours: float | null` — Task 4's Go `TicketIssueRef` unmarshals it.

- [ ] **Step 1: Write the failing test**

In `api/tests/test_v1_ticket_issues.py`, mirror the file's existing list-endpoint test seeding (it seeds `TicketIssueRun` rows via writers with an `issues` JSON list) and add:

```python
def test_ticket_issue_items_include_estimate_hours(client_and_db):
    client, db = client_and_db
    # Seed a run whose stored issues carry estimate_hours (and one without,
    # for the pre-rollout shape) using the same writer helper the neighboring
    # tests use, then:
    resp = client.get("/api/v1/ticket-issues")
    assert resp.status_code == 200
    items = resp.json()["items"][0]["issues"]
    assert items[0]["estimate_hours"] == 1.5
    assert items[1]["estimate_hours"] is None
```

(Copy the exact seeding lines from the adjacent test in the same file — the requirement is the two assertions.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_ticket_issues.py -k estimate_hours -v`
Expected: FAIL — `KeyError: 'estimate_hours'`

- [ ] **Step 3: Implement**

`api/app/queries/ticket_issues.py`, in the per-issue dict:

```python
                    {
                        "number": i.get("number"),
                        "title": i.get("title", ""),
                        "url": i.get("url"),
                        "state": i.get("state"),
                        "estimate_hours": i.get("estimate_hours"),
                    }
```

Update the docstring line 19 to `{number, title, url, state, estimate_hours}`. Leave `parent_issue` untouched (the epic carries no per-issue estimate). Add `estimate_hours: float | None = None` to the issue-ref response schema class in `api/app/schemas/ticket_issues.py` (the model validating those dicts — find it via the route's `response_model`).

- [ ] **Step 4: Run the API suite**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_ticket_issues.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add api/app/queries/ticket_issues.py api/app/schemas/ticket_issues.py api/tests/test_v1_ticket_issues.py
git commit -m "feat(api): expose per-issue estimate_hours on ticket-issue runs"
```

---

### Task 4: TUI — estimates on the tickets detail issue list

**Files:**
- Modify: `tui/internal/api/types.go` (`TicketIssueRef` ~line 194)
- Modify: `tui/internal/ui/tickets.go` (issue-row rendering ~line 684-698)
- Modify: `tui/internal/api/mock.go` (add `EstimateHours` to the existing `TicketIssueRef` mock entries — grep `TicketIssueRef{` for the spots)

**Interfaces:**
- Consumes: Task 3's JSON (`estimate_hours: float | null` per issue).

- [ ] **Step 1: Add the field**

`tui/internal/api/types.go`, in `TicketIssueRef`:

```go
type TicketIssueRef struct {
	Number        *int     `json:"number"`
	Title         string   `json:"title"`
	URL           *string  `json:"url"`
	State         *string  `json:"state"`
	EstimateHours *float64 `json:"estimate_hours"`
}
```

- [ ] **Step 2: Render it**

`tui/internal/ui/tickets.go`, in the detail issue-row loop (~line 684), suffix the title:

```go
		title := ref.Title
		if ref.EstimateHours != nil {
			title = fmt.Sprintf("%s  · %.1fh", title, *ref.EstimateHours)
		}
		line := fmt.Sprintf("  %-6s  %-12s  %s", num, state, title)
```

(Replace the existing `line := fmt.Sprintf("  %-6s  %-12s  %s", num, state, ref.Title)`.)

- [ ] **Step 3: Mock data**

In `tui/internal/api/mock.go`, give at least two mocked `TicketIssueRef` entries an estimate, e.g. `EstimateHours: f64Ptr(1.5),` (reuse the file's existing float-pointer helper; if the tickets mock lacks one in scope, use a small local `func(f float64) *float64` literal consistent with the file's style).

- [ ] **Step 4: Gate**

Run: `cd tui && go build ./... && go vet ./... && go test ./... && gofmt -l .`
Expected: build/vet/test green; `gofmt -l` prints nothing.

- [ ] **Step 5: Commit**

```bash
git add tui/internal/api/types.go tui/internal/api/mock.go tui/internal/ui/tickets.go
git commit -m "feat(tui): show per-issue dev estimates on tickets detail"
```

---

### Task 5: Verification sweep + docs sync

**Files:**
- Modify: `docs/superpowers/specs/2026-07-10-issue-estimates-to-odoo-design.md` (Status line + Context correction)

- [ ] **Step 1: Full gates** (shared `reva/` touched → all three services)

Run: `make test` → worker/api/scheduler all green.
Run: `ruff check reva worker/worker api/app scheduler/scheduler` → clean.
Run: `mypy reva worker/worker api/app scheduler/scheduler --ignore-missing-imports` → no NEW errors vs. main (advisory).
Run: `cd tui && go build ./... && go vet ./... && go test ./...` → green.

- [ ] **Step 2: Spec sync**

In the spec: replace the Status line with
`**Status:** Approved (Joseph, 2026-07-10) — implemented; see plans/2026-07-10-issue-estimates-to-odoo.md.`
and fix the Context bullet claiming "the union snapshot builder passes them through" to note the union builder needed the key added (implemented in this plan's Task 2) — code wins over prose.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-07-10-issue-estimates-to-odoo-design.md
git commit -m "docs(specs): issue estimates to Odoo — mark implemented, correct union-builder claim"
```

**Honest-status note for the final report:** REVA-side only — the Odoo ticket UI shows nothing new until the coordinated ast-odoo addon update (`issue_estimate_hours` field + total + "Dev estimate (low-end, AI-assisted)" label) ships; sending the fields early is safe (the addon ignores unknown keys). All coverage is unit-level (SQLite + mocked HTTP); the live callback shape lands on staging with the next real create-issues cycle.
