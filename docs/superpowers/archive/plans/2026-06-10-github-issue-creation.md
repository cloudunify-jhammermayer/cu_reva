# GitHub Issue Creation from Odoo Tickets — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement REVA's side of the github-issues handoff: accept `POST /api/v1/create-issues` from Odoo, plan 1–10 GitHub issues with Claude, create them (deduped across re-runs), and report back via `POST {odoo}/api/reva/issues-created`.

**Architecture:** A parallel pipeline structurally mirroring ticket analysis (route → DB row → RQ job → pure Claude planner → side-effecting runner), but conforming exactly to the contract in `ast-odoo/custom_addons/cu_reva_ticket_analysis/docs/github-issues-handoff.md`: the Odoo side is shipped and tested, so REVA adapts to it. Completion/failure is always reported through the new `issues-created` callback (never a field write); re-runs reconcile against a stable ticket-level marker embedded in issue bodies; partial failures resume from a persisted plan.

**Tech Stack:** FastAPI + RQ + SQLAlchemy (SQLite in tests, Postgres in prod), httpx, Claude Messages API with forced tool_use.

**Constraints from the user:** Do NOT commit. (Commit steps are intentionally absent.)

---

## Design corrections vs. the original proposal (verified against the handoff + Odoo addon)

| Original proposal | Corrected (why) |
|---|---|
| `POST /api/v1/ticket-issues`, body `{ticket_id, model_name, field_name, text, github_url}` | `POST /api/v1/create-issues`, body = Contract 1's exact 8 fields `{ticket_id, model_name, github_url, name, description, analysis_html, priority, ticket_url}` — Odoo hardcodes the path (`reva_mixin.py:205`) and payload (`:206-216`) |
| 202 returns `{analysis_id, job_id, status}` | 202 must contain `request_id` (int) — Odoo reads `data["request_id"]` and rolls back without it (`reva_mixin.py:235`) |
| Result: write HTML to Odoo `field_name` | Result: `POST {odoo}/api/reva/issues-created` with `{ticket_id, model_name, request_id, status, issues:[{number,title,url}], error}` — Odoo renders links itself (`reva_mixin.py:_apply_reva_issues`); no formatter, no field write |
| No failure callback | Every failure sends `status:"failed"` (best-effort) — otherwise Odoo's record is stuck `pending` with the button hidden forever (`reva_router.py:132`, only the callback transitions the state) |
| Per-issue dedup marker hashed over the Claude-generated title | Ticket-level marker `revaticket<sha1(owner/repo,model,ticket_id)[:16]>` — titles drift on re-plan; re-runs **reconcile** (return existing issues so Odoo re-links them), per the handoff's recommendation |
| "Requeue is safe because of dedup markers" | Requeue resumes the **persisted plan** (creates only items without an issue number); its callback only lands while Odoo still waits on the same `request_id`, else 409 (logged, run stays completed) — documented as ops-only |
| Transient errors leave the row pending for RQ retry | No RQ retry is configured anywhere in this pipeline (verified); transient failures also mark the run failed + send the failed callback |
| Optional `github_url` on `TicketAnalysisRequest` + column | Dropped — nothing sends it (`action_analyse_reva` payload has no `github_url`); speculative dead schema |
| `summary` field in the tool schema | Dropped — its only consumer was the deleted HTML write-back |

---

### Task 1: Migration 012 + `TicketIssueRun` model

**Files:**
- Create: `db/migrations/012_ticket_issue_runs.sql`
- Modify: `reva/db/models.py` (after `TicketAnalysis`, ~line 358)

- [ ] **Step 1: Write the migration**

```sql
CREATE TABLE ticket_issue_runs (
    id                    BIGSERIAL PRIMARY KEY,
    job_id                TEXT,
    ticket_id             INTEGER NOT NULL,
    model_name            TEXT NOT NULL,
    github_url            TEXT NOT NULL,
    name                  TEXT NOT NULL,
    description           TEXT NOT NULL,
    analysis_html         TEXT NOT NULL,
    priority              TEXT NOT NULL,
    ticket_url            TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending',
    issues                JSONB,
    error_message         TEXT,
    model                 TEXT,
    input_tokens          INTEGER DEFAULT 0,
    output_tokens         INTEGER DEFAULT 0,
    cache_read_tokens     INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    estimated_cost_usd    NUMERIC(12, 6) DEFAULT 0,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at          TIMESTAMPTZ
);

CREATE UNIQUE INDEX idx_ticket_issue_runs_job_id
    ON ticket_issue_runs (job_id)
    WHERE job_id IS NOT NULL;

CREATE INDEX idx_ticket_issue_runs_status    ON ticket_issue_runs (status);
CREATE INDEX idx_ticket_issue_runs_ticket_id ON ticket_issue_runs (ticket_id);
```

`issues` holds the persisted plan and its creation progress: `[{"title", "body", "acceptance_criteria", "number", "url"}]` (`number`/`url` null until created). The row id doubles as the Contract 1 `request_id` (BIGSERIAL ≥ 1, never colliding with Odoo's `reva_issue_request_id` default of 0).

- [ ] **Step 2: Add the SQLAlchemy model** (mirrors `TicketAnalysis`; `JSON` maps to JSONB on Postgres per models.py header)

```python
# ------------------------------------------------------- ticket_issue_runs


class TicketIssueRun(Base):
    __tablename__ = "ticket_issue_runs"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(Text)
    ticket_id: Mapped[int] = mapped_column(Integer, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    github_url: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    analysis_html: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(Text, nullable=False)
    ticket_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    issues: Mapped[Any | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Partial UNIQUE index (migration 012): job_id is unique only when set.
        Index(
            "idx_ticket_issue_runs_job_id",
            "job_id",
            unique=True,
            postgresql_where=text("job_id IS NOT NULL"),
            sqlite_where=text("job_id IS NOT NULL"),
        ),
        Index("idx_ticket_issue_runs_status", "status"),
        Index("idx_ticket_issue_runs_ticket_id", "ticket_id"),
    )
```

- [ ] **Step 3: Verify** — `python -c "from reva.db.models import TicketIssueRun"` in the worker venv; covered further by Task 3's writer tests creating the schema in SQLite.

---

### Task 2: Types — `TicketIssueItem`, `TicketIssuePlan`, `TicketIssueJobParams`

**Files:**
- Modify: `reva/types.py` (after the ticket-analysis section, ~line 245)

- [ ] **Step 1: Add the models**

```python
# --- Ticket issue creation types -----------------------------------------------


class TicketIssueItem(BaseModel):
    """One GitHub issue planned from an Odoo ticket."""

    title: str = Field(max_length=200)
    body: str
    acceptance_criteria: list[str] = Field(default_factory=list)

    @field_validator("title", mode="before")
    @classmethod
    def _truncate_title(cls, v: object) -> object:
        if isinstance(v, str) and len(v) > 200:
            return v[:197] + "..."
        return v

    @field_validator("acceptance_criteria", mode="before")
    @classmethod
    def _parse_json_string_list(cls, v: object) -> object:
        """Claude occasionally returns list fields as JSON strings; unwrap them."""
        if isinstance(v, str):
            import json
            return json.loads(v)
        return v


class TicketIssuePlan(BaseModel):
    """Structured output from the submit_ticket_issues tool_use call."""

    issues: list[TicketIssueItem] = Field(min_length=1, max_length=10)

    @field_validator("issues", mode="before")
    @classmethod
    def _parse_json_string_list(cls, v: object) -> object:
        """Claude occasionally returns list fields as JSON strings; unwrap them."""
        if isinstance(v, str):
            import json
            return json.loads(v)
        return v


class TicketIssueJobParams(BaseModel):
    """Inputs handed to the create-issues RQ job: the Contract 1 payload plus
    the ticket_issue_runs row id (which doubles as the Odoo request_id)."""

    run_id: int
    ticket_id: int
    model_name: str  # e.g. "helpdesk.ticket" or "project.task"
    github_url: str
    name: str
    description: str
    analysis_html: str  # "" when the record has no completed analysis
    priority: str  # Odoo priority key "0".."3"
    ticket_url: str
```

- [ ] **Step 2: Verify** — exercised by Task 5/6 tests (planner validation, bounds 1–10, JSON-string unwrap).

---

### Task 3: DB writers + PII purge (+ tests)

**Files:**
- Modify: `reva/db/writers.py` (new section after the ticket_analyses writers, ~line 916; import `TicketIssueRun`, `TicketIssueJobParams`)
- Test: `worker/tests/test_db.py` (append a ticket_issue_runs section)

- [ ] **Step 1: Write failing tests** — create/pending-dedup/get round-trip; `record_ticket_issue_plan` stores plan + usage + cost and returns cost; `update_ticket_issue_progress` persists numbers; completed/failed transitions; `reset_ticket_issue_run` keeps `issues`; `purge_old_ticket_issue_text` scrubs `description`+`analysis_html` only for old rows, keeps `issues`, is idempotent.
- [ ] **Step 2: Run** `pytest worker/tests/test_db.py -k ticket_issue -v` → FAIL (missing writers).
- [ ] **Step 3: Implement the writers** (full code in the workspace; signatures below — mirror the ticket_analyses section exactly):

```python
def record_ticket_issue_run_created(db, params: TicketIssueJobParams) -> int
def attach_ticket_issue_job_id(db, run_id: int, job_id: str) -> None
def get_pending_ticket_issue_run(db, ticket_id: int, model_name: str) -> dict | None
def get_ticket_issue_run(db, run_id: int) -> dict | None  # full row incl. inputs + issues
def record_ticket_issue_plan(db, run_id, issues: list[dict], response: ClaudeResponse) -> float
def update_ticket_issue_progress(db, run_id, issues: list[dict]) -> None
def record_ticket_issue_run_completed(db, run_id, issues: list[dict]) -> None
def record_ticket_issue_run_failed(db, run_id, error_message: str) -> None
def reset_ticket_issue_run(db, run_id) -> None   # keeps issues (the plan) for resume
def purge_old_ticket_issue_text(db, older_than_days: int) -> int  # PURGED_TICKET_TEXT sentinel
```

Notes: dedup key is `(ticket_id, model_name)` — no `field_name` in this contract. `record_ticket_issue_plan` persists plan/model/tokens/`estimate_cost(...)` while status stays `pending`, and returns the cost so the runner can ledger it via `record_claude_spend(db, "ticket_issues", cost)`.

- [ ] **Step 4: Run** the same tests → PASS.

---

### Task 4: Client methods — `OdooCallbackClient.issues_created`, `GitHubClient.find_issues_with_marker`, `reva/github_urls.py`

**Files:**
- Create: `reva/github_urls.py`
- Modify: `reva/odoo_client.py` (new method after `write_field`), `reva/github_client.py` (after `issue_exists_with_marker`, ~line 397)
- Test: `worker/tests/test_github_urls.py` (new), `worker/tests/test_odoo_client.py` (append), `worker/tests/test_github_client.py` (append)

- [ ] **Step 1: Failing tests.** URL parser: accepts `https://github.com/org/repo`, trailing `/`, `.git`; rejects `http://`, other hosts, extra path segments, empty. Odoo client: `issues_created` POSTs to `{base}/issues-created` with the exact Contract 2 payload and Bearer header; 409 → `PermanentError`; 5xx → `TransientError`. GitHub client: `find_issues_with_marker` queries `/search/issues` with `repo:{owner}/{repo} type:issue {marker}` (no `state:open` — closed issues must reconcile too) and maps items to `{number, title, url(html_url)}`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.**

```python
# reva/github_urls.py
"""Parse GitHub repository URLs from Odoo project settings. ..."""
_REPO_URL_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/"
    r"([A-Za-z0-9._-]+?)(?:\.git)?/?$"
)
def parse_github_repo_url(url: str) -> tuple[str, str] | None: ...
```

```python
# reva/odoo_client.py — uses the existing _post()/base-URL derivation; no config change
def issues_created(self, ticket_id, model_name, request_id, status, issues, error=None) -> None:
    self._post("/issues-created", {...Contract 2 payload...})
```

```python
# reva/github_client.py
def find_issues_with_marker(self, token, owner, repo, marker) -> list[dict]:
    response = self._get(token, "/search/issues",
                         params={"q": f"repo:{owner}/{repo} type:issue {marker}"})
    return [{"number": it["number"], "title": it["title"], "url": it["html_url"]}
            for it in response.json().get("items", [])]
```

- [ ] **Step 4: Run → PASS.**

---

### Task 5: Claude planner — tool, prompt, planner (+ tests)

**Files:**
- Create: `reva/ticket_issue_tool.py`, `reva/ticket_issue_planner.py`, `prompts/ticket_issues.md`, `worker/tests/fixtures/sample_ticket_issues.json`
- Test: `worker/tests/test_ticket_issue_planner.py`
- Modify: `prompts/CHANGELOG.md` (new `## v1.2` entry at top)

- [ ] **Step 1: Failing tests** (mirror `test_ticket_analyzer.py`, httpx.MockTransport): happy path returns a validated `TicketIssuePlan` (2 issues from fixture); name+description+analysis_html all nonce-wrapped and labelled untrusted; analysis section omitted when `analysis_html == ""`; no tool call → `PermanentError(match=TICKET_ISSUE_TOOL_NAME)`; invalid input (`{"issues": []}` — under min_length) → `PermanentError(match="validation")`; request carries `max_tokens=16384` and forced tool choice.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** tool module (schema derived from `TicketIssuePlan`, `$defs` passthrough, `additionalProperties: False`), planner (`plan_with_response`, SECU-5 nonce wrapping of name/description and — separately — analysis_html, `max_tokens=16384` because 10 full bodies can exceed review()'s 8192 default), prompt (REVA identity, same-language rule, split-only-when-independently-deliverable with single-issue fallback, ≤10, title/body/acceptance_criteria contract, never invent requirements, tool-call-only output).
- [ ] **Step 4: Run → PASS.**

---

### Task 6: Worker runner + context wiring (+ tests)

**Files:**
- Create: `worker/worker/ticket_issue_runner.py`, `worker/worker/ticket_issue_tasks.py` (stable RQ shim)
- Modify: `worker/worker/runner.py` (WorkerContext field `ticket_issue_planner: TicketIssuePlanner | None = None` among the defaulted fields + wiring in `build_worker_context`)
- Test: `worker/tests/test_ticket_issue_runner.py`

Runner contract (see design table; full code in workspace):

1. Parse owner/repo (PermanentError guard), `get_repo_installation_id` (PermanentError when app not installed), mint token, compute ticket-level marker `revaticket + sha1(owner/repo\x00model\x00ticket_id)[:16]`.
2. If the row has a persisted plan → resume it (requeue path, no Claude). Else search the marker: hits → **reconcile** (complete with the existing issues, callback re-links them; no Claude, no creation). Else: Claude plan → persist plan + usage + spend ledger **before any GitHub call**.
3. `ensure_label("reva-ticket")`; create each un-created item — body = plan body + acceptance-criteria checklist + **mandatory** `ticket_url` back-link + hidden marker comment; persist progress after each create.
4. Persist completed (issues JSONB) **before** the callback; then `odoo.issues_created(status="created", issues=[{number,title,url}...], request_id=run_id)`. Callback errors: log + re-raise, run stays completed (a 409 is the expected timeout-race outcome and must not undo the run).
5. **Any** failure before completion: record failed + best-effort `status:"failed"` callback (`issues:[]`, short error) + re-raise. TransientError included — no RQ retry exists in this pipeline, and Odoo can only leave `pending` via a callback.

- [ ] **Step 1: Failing tests** (SQLite DB + fakes for planner/GitHub/Odoo, mirroring `test_ticket_runner.py`): happy path (2 issues created with marker + ticket_url in body, label applied, row completed with issues, callback `created` echoing `request_id=run_id`, spend ledger row, never a `created` callback with empty issues); reconcile path (marker hit → no plan/create, callback carries existing issues); app-not-installed → failed row + failed callback + raise; planner TransientError → failed row + failed callback + raise; partial creation failure → first issue persisted with number, run failed, failed callback; then requeue (reset keeps plan) → only the missing issue created, callback carries the full set; callback PermanentError after creation → row stays completed + raise; failed-callback error never masks the original error.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement runner + shim + context wiring.**
- [ ] **Step 4: Run → PASS** (plus the whole worker suite for regressions: WorkerContext default keeps existing fixtures valid).

---

### Task 7: API — schemas, routes, registration (+ tests)

**Files:**
- Create: `api/app/schemas/ticket_issues.py`, `api/app/routes/v1/ticket_issues.py`
- Modify: `api/app/routes/v1/__init__.py` (import + `router.include_router(ticket_issues.router)`)
- Test: `api/tests/test_v1_ticket_issues.py`

Endpoints:
- `POST /api/v1/create-issues` (Contract 1) — 202 `{request_id, job_id, status}`; `github_url` validated via `parse_github_repo_url` → 422; pending-dedup on `(ticket_id, model_name)` returns the existing run's id (keeps the in-flight callback matching in Odoo); stub-row → enqueue `"worker.ticket_issue_tasks.run_ticket_issues"` (string path, CORR-1) → attach job id.
- `GET /api/v1/create-issues/{request_id}` — ops/debug; response model omits `description`/`analysis_html` (PII, mirrors `input_text` omission).
- `POST /api/v1/create-issues/{request_id}/requeue` — ops-only; 409 unless failed/completed; 409 when text purged and no plan persisted (would plan from the sentinel); resets row (keeping the plan) and re-enqueues.

Auth/rate-limit inherited from the v1 router — no per-route `Depends`.

- [ ] **Step 1: Failing tests**: the handoff's example payload **verbatim** → 202 with int `request_id` ≥ 1 + job enqueued with the exact string path; `analysis_html: ""` accepted; bad `github_url` variants → 422 and nothing enqueued; missing field → 422; dedup returns same `request_id` without second enqueue; GET 404 / GET hides PII fields; requeue 404/409/202 + reset-keeps-plan; purged-row requeue 409.
- [ ] **Step 2: Run → FAIL.** **Step 3: Implement.** **Step 4: Run → PASS.**

---

### Task 8: Scheduler PII purge wiring

**Files:**
- Modify: `scheduler/scheduler/main.py` (`maybe_purge_ticket_text` — add the `purge_old_ticket_issue_text` call + log)
- Test: scheduler tests if `maybe_purge_ticket_text` is covered there (check `scheduler/tests/`)

- [ ] Add alongside the existing purge call:

```python
    purged_issue_rows = writers.purge_old_ticket_issue_text(db, retention_days)
    if purged_issue_rows:
        logger.info("ticket_issue_text_purged", rows=purged_issue_rows,
                    retention_days=retention_days)
```

---

### Task 9: Docs — the requested README

**Files:**
- Create: `docs/github-issue-creation.md`

Contents: feature overview + sequence diagram; Contract 1/2 conformance table; state machines on both sides; dedup/reconcile semantics and the marker; failure-mode table (incl. timeout race, partial creation, callback 409); requeue semantics and limits; DB schema; PII retention; config (no new env vars — `ODOO_CALLBACK_URL` reused via base-URL derivation; note both API keys must be set together); how to run the tests; known limitations (re-click after partial failure reconciles to the partial set — ops requeue completes it; GitHub search eventual consistency).

---

### Task 10: Full verification

- [ ] `worker/.venv/bin/pytest worker/tests -q` → all pass
- [ ] `api/.venv/bin/pytest api/tests -q` → all pass
- [ ] scheduler tests if present → pass
- [ ] Grep for orphans introduced by this change (unused imports etc.)

## Self-review notes

- Spec coverage: Contract 1 (Task 7), Contract 2 incl. failure + response-code table (Tasks 4/6), dedup/reconcile (Tasks 4/6), split-basis = analysis sections (Task 5), labels/priority discretion (label yes, priority mapping deliberately omitted — allowed by handoff), PII retention (Tasks 3/8), no speculative `github_url`-on-analysis / no `summary` field / no HTML formatter (dropped by design).
- The 202 key is `request_id` everywhere (route, dedup, requeue); the runner echoes `params.run_id` as `request_id` in both callbacks.
- `TicketIssueJobParams` field names exactly match `CreateIssuesRequest` so `TicketIssueJobParams(run_id=..., **body.model_dump())` works.
