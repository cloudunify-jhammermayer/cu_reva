# Typed Issue Requests + Unified Title Convention — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Odoo can send an ad-hoc, optionally-typed issue request (wizard text → 1..N GitHub issues); all REVA-created issues adopt the `[TYPE] <ticket_id> - <tldr>` title convention, type labels, one-epic-per-ticket attachment, and union-snapshot callbacks so multiple requests per ticket accumulate instead of wiping each other.

**Architecture:** No new endpoint — `POST /api/v1/create-issues` gains one optional `issue_type` field and the existing `ticket_issue_runs` pipeline handles both flows. The Odoo wizard sends a normal Contract-1 payload with `description` = wizard text. Both Odoo callbacks (`issues-created`, `issue-state`) switch to sending the union of issues across all of the ticket's runs. Spec: `docs/superpowers/specs/2026-07-02-typed-issue-requests-design.md`.

**Tech Stack:** FastAPI + Pydantic + SQLAlchemy (cu_reva), RQ worker, Anthropic Messages API (planner), Go/Bubble Tea (tui), Odoo 19 addon with OCA-FastAPI router (ast-odoo).

## Global Constraints

- Type codes are exactly `("BUG", "FEAT", "CR", "CONF", "DEV", "MIG", "SUP", "DOC")`. No FB.
- Title: `[{TYPE}] {ticket_id} - {tldr}` + ` ({n}/{total})` only when the request yields ≥ 2 issues. tldr hard-truncated to 30 chars (`title[:30].rstrip()`).
- Plan items persisted before this rollout have no `type` → fall back to `"DEV"`.
- Parent/epic title: `[{dominant-type}] {ticket_id} - {name[:30]}`, no `(n/total)`. Dominant = most common child type, tie → first child's.
- One epic per ticket+repo: adopt an existing parent from any prior run before creating one; create only when none exists AND this request yields ≥ 2 issues.
- Callbacks send the **union** of issues (with `state`) across all runs matching `(odoo_instance_id, ticket_id, model_name)`, deduped by number, newest run wins.
- `cu_reva` and `ast-odoo` are **separate git repositories** — commit each task in the repo it touches. cu_reva commits end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Test commands: `cd worker && .venv/bin/python -m pytest tests/...` (same pattern for `api/`); full gate = `make test` + `ruff check reva worker/worker api/app scheduler/scheduler` + `cd tui && go build ./... && go vet ./... && go test ./...`. Addon: from `/home/joseph/Projects/Cloudunify/ast-odoo`: `uv run odoo/odoo-bin -d <testdb> -u cu_reva_ticket_analysis --test-tags cu_reva --stop-after-init --http-port 8169 --workers 0` (check ast-odoo root CLAUDE.md/README if the db/addons-path flags differ).
- Deploy order (after merge): REVA first, then the addon.

---

### Task 1: Type plumbing — `reva/types.py`, migration 023, basic writers

**Files:**
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/reva/types.py` (~line 294 onward)
- Create: `/home/joseph/Projects/Cloudunify/cu_reva/db/migrations/023_ticket_issue_type.sql`
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/reva/db/models.py:451` (after `planning_basis`)
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/reva/db/writers.py` (`compute_planning_basis` ~1164, `record_ticket_issue_run_created` ~1196, `get_ticket_issue_run` ~1255)
- Test: `/home/joseph/Projects/Cloudunify/cu_reva/worker/tests/test_ticket_issue_writers.py`

**Interfaces:**
- Consumes: existing `TicketIssueJobParams`, `TicketIssueItem`, writers.
- Produces: `ISSUE_TYPE_CODES: tuple[str, ...]` and `IssueTypeCode` (Literal) in `reva.types`; `TicketIssueItem.type: IssueTypeCode = "DEV"`; `TicketIssueJobParams.issue_type: str | None = None`; `ticket_issue_runs.issue_type` column; `compute_planning_basis` returns `"cr:text:<sha1>"`-style values for typed requests; `get_ticket_issue_run(...)["issue_type"]`.

- [ ] **Step 1: Write the failing tests** — append to `worker/tests/test_ticket_issue_writers.py`, reusing its existing imports/fixtures (it builds a SQLite `Database` like `test_ticket_issue_runner.py`; reuse its params helper if one exists, else add `_typed_params` below):

```python
def _typed_params(**overrides):
    from reva.types import TicketIssueJobParams
    base = dict(
        run_id=0, odoo_instance_id=1, ticket_id=77, model_name="helpdesk.ticket",
        github_url="https://github.com/org/repo", name="Ticket name",
        description="Change the delivery slip layout", analysis_html="",
        priority="1", ticket_url="https://odoo.example.com/web#id=77",
    )
    base.update(overrides)
    return TicketIssueJobParams(**base)


def test_planning_basis_typed_prefix():
    untyped = writers.compute_planning_basis(_typed_params())
    typed = writers.compute_planning_basis(_typed_params(issue_type="CR"))
    assert untyped.startswith("text:")          # pre-type format unchanged
    assert typed == "cr:" + untyped             # type folded in front


def test_issue_type_persisted_roundtrip(db):
    run_id = writers.record_ticket_issue_run_created(db, _typed_params(issue_type="CR"))
    assert writers.get_ticket_issue_run(db, run_id)["issue_type"] == "CR"
    # untyped stays NULL (different ticket: one pending run per record)
    run_id2 = writers.record_ticket_issue_run_created(db, _typed_params(ticket_id=78))
    assert writers.get_ticket_issue_run(db, run_id2)["issue_type"] is None
```

(`db` = the file's existing SQLite Database fixture; if the file has none, add one modeled on `ctx_and_fakes` in `worker/tests/test_ticket_issue_runner.py:141-145`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_writers.py -k "typed_prefix or roundtrip" -v`
Expected: FAIL (`issue_type` unexpected keyword / KeyError).

- [ ] **Step 3: Implement**

`reva/types.py` — above `TicketIssueItem` (~line 297):

```python
# Work-item type codes: title prefix + GitHub label on every REVA-created issue.
ISSUE_TYPE_CODES = ("BUG", "FEAT", "CR", "CONF", "DEV", "MIG", "SUP", "DOC")
IssueTypeCode = Literal["BUG", "FEAT", "CR", "CONF", "DEV", "MIG", "SUP", "DOC"]
```

`TicketIssueItem` — add after `acceptance_criteria` (line 302):

```python
    # Defaults to DEV so plans persisted before the type rollout still
    # validate; the runner overrides it when the request fixes a type.
    type: IssueTypeCode = "DEV"
```

`TicketIssueJobParams` — add after `ticket_url` (line 344):

```python
    # Fixed work-item type for every issue of this request (Odoo wizard), or
    # None to let the planner pick per issue (analysis flow).
    issue_type: str | None = None
```

`db/migrations/023_ticket_issue_type.sql`:

```sql
-- Optional fixed work-item type for a create-issues request (Odoo wizard flow).
-- NULL = the planner picks a type per issue (analysis flow and pre-type rows).
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS issue_type TEXT;
```

`reva/db/models.py` — after `planning_basis` (line 451):

```python
    # Fixed work-item type for this request ("CR", "BUG", …; migration 023),
    # or NULL when the planner picks per issue.
    issue_type: Mapped[str | None] = mapped_column(Text)
```

`reva/db/writers.py` `compute_planning_basis` — replace the final `return prefix + digest` with:

```python
    basis = prefix + digest
    if params.issue_type:
        # A typed request plans separately from an untyped one over the same
        # text (own marker, no cross-adoption); untyped runs keep the pre-type
        # basis format so existing markers stay valid.
        return params.issue_type.lower() + ":" + basis
    return basis
```

`record_ticket_issue_run_created` — add `issue_type=params.issue_type,` to the `TicketIssueRun(...)` kwargs (after `planning_basis=...`). `get_ticket_issue_run` — add `"issue_type": row.issue_type,` after `"planning_basis"`.

- [ ] **Step 4: Run tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_writers.py -v`
Expected: PASS (all, incl. pre-existing).

- [ ] **Step 5: Commit** (in cu_reva)

```bash
git add reva/types.py reva/db/models.py reva/db/writers.py db/migrations/023_ticket_issue_type.sql worker/tests/test_ticket_issue_writers.py
git commit -m "feat(issues): issue_type plumbing — types, migration 023, typed planning basis"
```

---

### Task 2: Union + parent-adoption writers

**Files:**
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/reva/db/writers.py` (after `update_ticket_issue_state`, ~line 1463)
- Test: `/home/joseph/Projects/Cloudunify/cu_reva/worker/tests/test_ticket_issue_writers.py`

**Interfaces:**
- Consumes: `TicketIssueRun` ORM, Task 1's `_typed_params` helper.
- Produces:
  - `get_ticket_issue_union(db, odoo_instance_id: int | None, ticket_id: int, model_name: str) -> list[dict]` — items `{"number": int, "title": str, "url": str | None, "state": "open"|"closed"}`, sorted by number, deduped (newest run wins), only items with a GitHub number.
  - `get_latest_ticket_issue_parent(db, odoo_instance_id: int | None, ticket_id: int, model_name: str, repo_full_name: str, exclude_run_id: int) -> dict | None` — the newest other run's `parent_issue` dict for this record+repo.

- [ ] **Step 1: Write the failing tests**

```python
def _complete_run(db, params, issues):
    run_id = writers.record_ticket_issue_run_created(db, params)
    writers.update_ticket_issue_progress(db, run_id, issues)
    writers.record_ticket_issue_run_completed(db, run_id, issues)
    return run_id


def test_union_dedups_newest_wins_and_scopes_by_instance(db):
    p = _typed_params(ticket_id=90)
    _complete_run(db, p, [
        {"number": 1, "title": "old title", "url": "https://gh/1", "state": "closed"},
        {"number": 2, "title": "two", "url": "https://gh/2", "state": "open"},
        {"number": None, "title": "never created", "url": None, "state": None},
    ])
    _complete_run(db, p, [
        {"number": 1, "title": "new title", "url": "https://gh/1", "state": "open"},
        {"number": 3, "title": "three", "url": "https://gh/3", "state": "open"},
    ])
    # same ticket id on ANOTHER instance must not leak in
    _complete_run(db, _typed_params(ticket_id=90, odoo_instance_id=2),
                  [{"number": 99, "title": "other", "url": "https://gh/99", "state": "open"}])

    union = writers.get_ticket_issue_union(db, 1, 90, "helpdesk.ticket")
    assert [i["number"] for i in union] == [1, 2, 3]
    assert union[0]["title"] == "new title"      # newest run wins
    assert union[1]["state"] == "open"


def test_latest_parent_scoped_and_excludes_self(db):
    p = _typed_params(ticket_id=91)
    r1 = _complete_run(db, p, [{"number": 5, "title": "t", "url": "https://gh/5", "state": "open"}])
    parent = {"number": 4, "id": 900004, "url": "https://gh/4", "title": "[DEV] 91 - Epic", "state": "open"}
    writers.set_ticket_issue_parent(db, r1, parent)

    got = writers.get_latest_ticket_issue_parent(
        db, 1, 91, "helpdesk.ticket", "org/repo", exclude_run_id=999)
    assert got == parent
    # own run excluded; other repo/instance → None
    assert writers.get_latest_ticket_issue_parent(db, 1, 91, "helpdesk.ticket", "org/repo", exclude_run_id=r1) is None
    assert writers.get_latest_ticket_issue_parent(db, 1, 91, "helpdesk.ticket", "org/other", exclude_run_id=999) is None
    assert writers.get_latest_ticket_issue_parent(db, 2, 91, "helpdesk.ticket", "org/repo", exclude_run_id=999) is None
```

- [ ] **Step 2: Run to verify failure** — `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_writers.py -k "union or latest_parent" -v` → FAIL (AttributeError).

- [ ] **Step 3: Implement** — append to `reva/db/writers.py` after `update_ticket_issue_state`:

```python
def _instance_filter(odoo_instance_id: int | None):
    if odoo_instance_id is None:
        return TicketIssueRun.odoo_instance_id.is_(None)
    return TicketIssueRun.odoo_instance_id == odoo_instance_id


def get_ticket_issue_union(
    db: Database, odoo_instance_id: int | None, ticket_id: int, model_name: str
) -> list[dict]:
    """Union of created issues across ALL runs for this record, deduped by
    issue number (newest run wins title/url/state), sorted by number.

    The Odoo issues-created handler replaces the record's whole issue list
    with the payload — sending only the completing run's issues would wipe
    what earlier requests created (wizard + planner requests accumulate).
    Parents are excluded (parent_issue column, never in `issues`)."""
    from sqlalchemy.orm import load_only

    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.ticket_id == ticket_id,
                TicketIssueRun.model_name == model_name,
                _instance_filter(odoo_instance_id),
                TicketIssueRun.issues.is_not(None),
            )
            .options(load_only(TicketIssueRun.issues, TicketIssueRun.created_at))
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
        ).scalars().all()
        seen: dict[int, dict] = {}
        for row in rows:  # newest first — first occurrence of a number wins
            for item in row.issues or []:
                n = item.get("number")
                if n is None or n in seen:
                    continue
                seen[n] = {
                    "number": n,
                    "title": item.get("title", ""),
                    "url": item.get("url"),
                    "state": item.get("state") or "open",
                }
        return sorted(seen.values(), key=lambda i: i["number"])


def get_latest_ticket_issue_parent(
    db: Database,
    odoo_instance_id: int | None,
    ticket_id: int,
    model_name: str,
    repo_full_name: str,
    exclude_run_id: int,
) -> dict | None:
    """The record's existing parent ("epic") issue in this repo, from the most
    recent other run that has one — or None. One epic per ticket: a new run
    attaches its issues to this parent instead of creating a second one."""
    from sqlalchemy.orm import load_only

    with db.session() as s:
        row = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.ticket_id == ticket_id,
                TicketIssueRun.model_name == model_name,
                TicketIssueRun.repo_full_name == repo_full_name,
                _instance_filter(odoo_instance_id),
                TicketIssueRun.parent_issue.is_not(None),
                TicketIssueRun.id != exclude_run_id,
            )
            .options(load_only(TicketIssueRun.parent_issue, TicketIssueRun.created_at))
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return dict(row.parent_issue) if row is not None else None
```

- [ ] **Step 4: Run tests** — same command → PASS.

- [ ] **Step 5: Commit**

```bash
git add reva/db/writers.py worker/tests/test_ticket_issue_writers.py
git commit -m "feat(issues): union-snapshot and cross-run parent lookup writers"
```

---

### Task 3: API — accept optional `issue_type`

**Files:**
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/api/app/schemas/ticket_issues.py:12-31`
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/api/app/routes/v1/ticket_issues.py:265-279` (requeue params)
- Test: `/home/joseph/Projects/Cloudunify/cu_reva/api/tests/test_v1_ticket_issues.py`

**Interfaces:**
- Consumes: Task 1's `TicketIssueJobParams.issue_type`, `get_ticket_issue_run()["issue_type"]`.
- Produces: `CreateIssuesRequest.issue_type: Literal[8 codes] | None = None` ("" coerced to None). `submit_create_issues` passes it through automatically (`**body.model_dump()`), requeue re-sends the stored value.

- [ ] **Step 1: Write the failing tests** — append to `api/tests/test_v1_ticket_issues.py` (fixture `client_db_queue` yields `(tc, db, queue, headers)`):

```python
def test_create_issues_accepts_issue_type(client_db_queue):
    tc, db, queue, headers = client_db_queue
    resp = tc.post("/api/v1/create-issues",
                   json={**CONTRACT_PAYLOAD, "issue_type": "CR"}, headers=headers)
    assert resp.status_code == 202
    _, params, _ = queue.enqueued[0]
    assert params["issue_type"] == "CR"
    assert writers.get_ticket_issue_run(db, resp.json()["request_id"])["issue_type"] == "CR"


def test_create_issues_rejects_unknown_issue_type(client_db_queue):
    tc, _, _, headers = client_db_queue
    resp = tc.post("/api/v1/create-issues",
                   json={**CONTRACT_PAYLOAD, "issue_type": "FB"}, headers=headers)
    assert resp.status_code == 422


def test_create_issues_empty_issue_type_is_untyped(client_db_queue):
    tc, _, queue, headers = client_db_queue
    resp = tc.post("/api/v1/create-issues",
                   json={**CONTRACT_PAYLOAD, "issue_type": ""}, headers=headers)
    assert resp.status_code == 202
    _, params, _ = queue.enqueued[0]
    assert params["issue_type"] is None


def test_requeue_preserves_issue_type(client_db_queue):
    tc, db, queue, headers = client_db_queue
    resp = tc.post("/api/v1/create-issues",
                   json={**CONTRACT_PAYLOAD, "issue_type": "BUG"}, headers=headers)
    request_id = resp.json()["request_id"]
    writers.record_ticket_issue_run_failed(db, request_id, "boom")
    resp = tc.post(f"/api/v1/create-issues/{request_id}/requeue")
    assert resp.status_code == 202
    _, params, _ = queue.enqueued[-1]
    assert params["issue_type"] == "BUG"
```

(The requeue route sits behind the master-key gate, not the instance key — check how this file's existing requeue tests authenticate that call and copy their header/override pattern.)

- [ ] **Step 2: Verify failure** — `cd api && .venv/bin/python -m pytest tests/test_v1_ticket_issues.py -k issue_type -v` → FAIL (`issue_type` missing from enqueued params / 202 vs 422).

- [ ] **Step 3: Implement**

`api/app/schemas/ticket_issues.py` — extend imports: `from typing import Literal`, `from pydantic import BaseModel, Field, field_validator`. In `CreateIssuesRequest`, after `ticket_url` (note the class docstring says the field set is fixed — update it: fixed for *required* fields; optional additive fields are fine):

```python
    issue_type: Literal["BUG", "FEAT", "CR", "CONF", "DEV", "MIG", "SUP", "DOC"] | None = Field(
        default=None,
        description="Fixed work-item type for every issue of this request "
        "(Odoo wizard flow). Omitted/empty: the planner picks per issue.",
    )

    @field_validator("issue_type", mode="before")
    @classmethod
    def _empty_type_is_none(cls, v: object) -> object:
        # The Odoo wizard's empty Selection may serialize as "" — treat as unset.
        return None if v == "" else v
```

`api/app/routes/v1/ticket_issues.py` `requeue_ticket_issue_run` — add to the `TicketIssueJobParams(...)` kwargs (after `ticket_url=row["ticket_url"],`):

```python
        issue_type=row["issue_type"],
```

(`submit_create_issues` needs no change: `stub`/`params` are built with `**body.model_dump()`.)

- [ ] **Step 4: Run tests** — `cd api && .venv/bin/python -m pytest tests/test_v1_ticket_issues.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add api/app/schemas/ticket_issues.py api/app/routes/v1/ticket_issues.py api/tests/test_v1_ticket_issues.py
git commit -m "feat(issues): accept optional issue_type on POST /api/v1/create-issues"
```

---

### Task 4: Planner — per-issue `type`, ≤30-char tldr, fixed-type instruction

**Files:**
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/prompts/ticket_issues.md`
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/prompts/CHANGELOG.md` (new `## v1.8` entry at top)
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/reva/ticket_issue_planner.py:67-123` (`_build_user_prompt`)
- Test: `/home/joseph/Projects/Cloudunify/cu_reva/worker/tests/test_ticket_issue_planner.py`

**Interfaces:**
- Consumes: `TicketIssueItem.type` (Task 1); tool schema auto-derives from `TicketIssuePlan`.
- Produces: user prompt ends with the fixed-type instruction when `params.issue_type` is set; tool schema exposes the 8-code `type` enum.

- [ ] **Step 1: Write the failing tests** — append to `worker/tests/test_ticket_issue_planner.py` (reuse its existing params/factory helpers for `TicketIssueJobParams`; if none fit, use `_typed_params` from Task 1 inline):

```python
def test_user_prompt_carries_fixed_type():
    typed = TicketIssuePlanner._build_user_prompt(_typed_params(issue_type="CR"))
    untyped = TicketIssuePlanner._build_user_prompt(_typed_params())
    assert 'set `type` to "CR" on every issue' in typed
    assert "set `type`" not in untyped


def test_tool_schema_exposes_type_enum():
    from reva.ticket_issue_tool import build_ticket_issue_tool_schema
    schema = build_ticket_issue_tool_schema()
    props = schema["input_schema"]["$defs"]["TicketIssueItem"]["properties"]
    assert props["type"]["enum"] == ["BUG", "FEAT", "CR", "CONF", "DEV", "MIG", "SUP", "DOC"]
```

- [ ] **Step 2: Verify failure** — `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_planner.py -k "fixed_type or type_enum" -v` → the schema test may already PASS (Task 1 added the field); the prompt test must FAIL.

- [ ] **Step 3: Implement**

`reva/ticket_issue_planner.py` `_build_user_prompt` — the docx branch currently `return`s directly (line 82). Restructure so both branches produce `sections` and share one typed tail; behavior for untyped requests stays byte-identical:

1. In the docx branch, replace `return "\n".join([` with `sections = [` (list content unchanged) and let it fall through past the `else`-less text branch by wrapping the text-branch section building in an `else:` — i.e. the method becomes:

```python
        nonce = secrets.token_hex(8)
        if params.description_docx is not None:
            attachment_text = extract_attachment_text(
                params.description_docx.filename, params.description_docx.content_base64
            )
            sections = [
                # ... existing docx-branch list content, unchanged ...
            ]
        else:
            sections = [
                # ... existing text-branch list content, unchanged ...
            ]
            if params.analysis_html:
                sections += [
                    # ... existing analysis block, unchanged ...
                ]
            else:
                sections += [
                    # ... existing no-analysis block, unchanged ...
                ]

        prompt = "\n".join(sections)
        if params.issue_type:
            prompt += (
                f'\n\nThis request is typed: set `type` to "{params.issue_type}" '
                "on every issue you plan."
            )
        return prompt
```

(The `# ... unchanged ...` lines are the exact existing list literals from lines 82-122 — move them, don't rewrite them.)

`prompts/ticket_issues.md` — in "What each issue must contain" replace the `title` bullet and add a `type` bullet:

```markdown
- `title` — a TLDR of the work: **at most 30 characters**, imperative, specific (e.g. "Add login form validation"). The system renders the full GitHub title itself (`[TYPE] <ticket_id> - <tldr> (n/total)`).
- `type` — the work-item code, exactly one of: `BUG` (defect fix), `FEAT` (new functionality), `CR` (change request to existing behaviour), `CONF` (configuration/setup), `DEV` (internal development/refactoring), `MIG` (migration), `SUP` (support task), `DOC` (documentation). When the request specifies a fixed type, set that type on every issue.
```

`prompts/CHANGELOG.md` — new entry at the very top:

```markdown
## v1.8 — Issue types + tldr titles for ticket-issue planning

- `ticket_issues.md`: each planned issue now carries a `type` code
  (`BUG`/`FEAT`/`CR`/`CONF`/`DEV`/`MIG`/`SUP`/`DOC`) and `title` is a
  ≤30-character tldr — the worker renders the full GitHub title
  (`[TYPE] <ticket_id> - <tldr> (n/total)`) and applies the type as a
  label. Typed requests (Odoo wizard) fix the type for every issue.
```

- [ ] **Step 4: Run tests** — `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_planner.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add prompts/ticket_issues.md prompts/CHANGELOG.md reva/ticket_issue_planner.py worker/tests/test_ticket_issue_planner.py
git commit -m "feat(issues): planner emits per-issue type; fixed-type instruction (prompt v1.8)"
```

---

### Task 5: Runner — new title convention + type labels

**Files:**
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/worker/worker/ticket_issue_runner.py`
- Test: `/home/joseph/Projects/Cloudunify/cu_reva/worker/tests/test_ticket_issue_runner.py`

**Interfaces:**
- Consumes: `TicketIssueJobParams.issue_type`, plan items with `.type`.
- Produces: child titles `[CR] 123 - <tldr> (1/2)` (`(n/total)` only when total ≥ 2); parent title `[DEV] 123 - <name tldr>`; issues created with labels `["reva-ticket", <TYPE>]`; plan items persist `"type"`; helpers `_item_type(params, item)`, `_dominant_type(params, issues)`, `_TYPE_LABELS`.

- [ ] **Step 1: Update existing tests + add new ones.** The title change breaks existing assertions in `test_ticket_issue_runner.py` — update them all to the new format (grep for `"[Ticket ` / `"[Task ` / `labels_ensured`). E.g. in `test_happy_path_creates_issues_and_calls_back`:

```python
    assert s["github"].labels_ensured == ["reva-ticket", "DEV"]
    ...
    assert s["github"].created[1]["labels"] == ["reva-ticket", "DEV"]
    ...
    assert cb["issues"] == [
        {"number": 102, "title": "[DEV] 123 - Issue 1 (1/2)",
         "url": "https://github.com/acme/widgets/issues/102"},
        {"number": 103, "title": "[DEV] 123 - Issue 2 (2/2)",
         "url": "https://github.com/acme/widgets/issues/103"},
    ]
```

(The fake plan has no explicit type → default `DEV`. Parent title assertions become `[DEV] 123 - Login page broken`.)

Extend `_make_params` to accept overrides (existing callers unchanged):

```python
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
```

New tests:

```python
def test_typed_single_issue_title_and_labels(ctx_and_fakes):
    s = ctx_and_fakes
    s["planner"].plan = TicketIssuePlan(issues=[TicketIssueItem(
        title="Adjust delivery slip layout that is way too long for a title",
        body="B", type="FEAT")])
    params = _make_params(s["db"], issue_type="CR", description="Change the layout")

    run_ticket_issues(params)

    created = s["github"].created
    assert len(created) == 1                      # single issue → no parent
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
```

- [ ] **Step 2: Verify failure** — `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_runner.py -v` → new tests FAIL, updated old assertions FAIL against current code.

- [ ] **Step 3: Implement** in `worker/worker/ticket_issue_runner.py`:

Add near `_TICKET_ISSUE_LABEL` (line 43), plus `from collections import Counter` to imports:

```python
# GitHub label per work-item type: name = the code itself (filter `label:CR`),
# ensured per-repo on demand. (color, description).
_TYPE_LABELS = {
    "BUG": ("d73a4a", "Bug fix"),
    "FEAT": ("a2eeef", "New feature"),
    "CR": ("0075ca", "Change request"),
    "CONF": ("bfd4f2", "Configuration change"),
    "DEV": ("7057ff", "Development task"),
    "MIG": ("fbca04", "Migration"),
    "SUP": ("008672", "Support"),
    "DOC": ("0e8a16", "Documentation"),
}
_FALLBACK_TYPE = "DEV"  # plans persisted before the type rollout carry no type
_TLDR_MAX = 30
```

Replace `_issue_title` (96-104) and `_parent_title` (127-131):

```python
def _item_type(params: TicketIssueJobParams, item: dict) -> str:
    """A typed request fixes every issue's type; else the planner's pick,
    falling back to DEV for pre-rollout persisted plans."""
    return params.issue_type or item.get("type") or _FALLBACK_TYPE


def _issue_title(params: TicketIssueJobParams, position: int, total: int,
                 title: str, issue_type: str) -> str:
    """GitHub issue title: '[CR] 2010 - <tldr>' plus ' (3/10)' when the request
    yields several issues. The ticket id makes every issue traceable to its
    ticket from the GitHub list alone; the planner is prompted for a ≤30-char
    tldr and the slice is the hard backstop."""
    seq = f" ({position}/{total})" if total >= 2 else ""
    return f"[{issue_type}] {params.ticket_id} - {title[:_TLDR_MAX].rstrip()}{seq}"


def _dominant_type(params: TicketIssueJobParams, issues: list[dict]) -> str:
    """Most common child type; tie → the first child's."""
    types = [_item_type(params, i) for i in issues]
    counts = Counter(types)
    best = max(counts.values())
    return next(t for t in types if counts[t] == best)


def _parent_title(params: TicketIssueJobParams, dominant: str) -> str:
    """Parent ("epic") title: '[FEAT] 2010 - <ticket-name tldr>' — dominant
    child type, no (n/total) order marker."""
    return f"[{dominant}] {params.ticket_id} - {params.name[:_TLDR_MAX].rstrip()}"
```

In `_plan_and_create`:

1. Plan mapping (~line 370): add `"type": params.issue_type or item.type,` after `"acceptance_criteria"`.
2. Label ensuring (~line 397): after the existing `ensure_label(... _TICKET_ISSUE_LABEL ...)` call add:

```python
    pending_types = {_item_type(params, i) for i in issues if i.get("number") is None}
    if need_parent and parent is None:
        pending_types.add(_dominant_type(params, issues))
    for type_code in sorted(pending_types):
        color, desc = _TYPE_LABELS.get(type_code, ("ededed", ""))
        ctx.github.ensure_label(token, owner, repo, type_code, color=color, description=desc)
```

(Move this below the `need_parent` computation at line 393 so `need_parent` is defined.)

3. Parent creation block (~line 403):

```python
    if need_parent and parent is None:
        dominant = _dominant_type(params, issues)
        parent_t = _parent_title(params, dominant)
        created = ctx.github.create_issue(
            token, owner, repo,
            title=parent_t,
            body=_format_parent_body(params, marker, parent_marker),
            labels=[_TICKET_ISSUE_LABEL, dominant],
        )
        parent = {"number": created["number"], "id": created["id"],
                  "url": created["url"], "title": parent_t, "state": "open"}
```

4. Child creation loop (~line 421): compute the type, pass it, keep it in the trimmed item:

```python
        issue_type = _item_type(params, item)
        title = _issue_title(params, idx + 1, len(issues), item["title"], issue_type)
        created = ctx.github.create_issue(
            token, owner, repo,
            title=title,
            body=_format_issue_body(item, params, marker),
            labels=[_TICKET_ISSUE_LABEL, issue_type],
        )
        issues[idx] = {
            "title": title,
            "type": issue_type,
            "number": created["number"],
            "id": created["id"],
            "url": created["url"],
            "state": "open",
            "attached": False,
        }
```

- [ ] **Step 4: Run tests** — `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_runner.py -v` → PASS (all).

- [ ] **Step 5: Commit**

```bash
git add worker/worker/ticket_issue_runner.py worker/tests/test_ticket_issue_runner.py
git commit -m "feat(issues): [TYPE] id - tldr title convention + type labels in runner"
```

---

### Task 6: Runner — one epic per ticket + union callbacks

**Files:**
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/worker/worker/ticket_issue_runner.py`
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/reva/odoo_client.py:106-159` (docstrings only)
- Test: `/home/joseph/Projects/Cloudunify/cu_reva/worker/tests/test_ticket_issue_runner.py`

**Interfaces:**
- Consumes: `get_ticket_issue_union`, `get_latest_ticket_issue_parent` (Task 2).
- Produces: `issues_created` callback payload = union items `{number,title,url,state}`; `issue_state` snapshot = same union; new runs adopt the ticket's existing parent (attach even a single issue); parent created only when none exists and ≥ 2 issues.

- [ ] **Step 1: Write failing tests + update the happy path.** In `test_happy_path_creates_issues_and_calls_back`, add `"state": "open"` to both expected callback items. New tests:

```python
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


def test_single_issue_without_epic_stays_flat(ctx_and_fakes):
    s = ctx_and_fakes
    s["planner"].plan = TicketIssuePlan(issues=[
        TicketIssueItem(title="One thing", body="B")])
    params = _make_params(s["db"])
    run_ticket_issues(params)
    assert len(s["github"].created) == 1
    assert s["github"].sub_issues == []


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
```

- [ ] **Step 2: Verify failure** — `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_runner.py -k "epic or union or flat" -v` → FAIL.

- [ ] **Step 3: Implement** in `ticket_issue_runner.py`:

1. **Parent adoption** — in `_plan_and_create`, right after the plan-adoption block (after line 329, before the early-exit `if issues is not None:` block):

```python
    if parent is None:
        # One epic per ticket: adopt the parent an earlier run created in this
        # repo so new issues (wizard requests, re-plans over changed text)
        # attach to the existing epic instead of spawning a second one.
        prior_parent = writers.get_latest_ticket_issue_parent(
            ctx.db, params.odoo_instance_id, params.ticket_id, params.model_name,
            f"{owner.lower()}/{repo.lower()}", exclude_run_id=params.run_id,
        )
        if prior_parent is not None:
            parent = prior_parent
            writers.set_ticket_issue_parent(ctx.db, params.run_id, parent)
            log.info("ticket_issue_parent_adopted", issue=parent.get("number"))
```

2. **`need_parent` formula** — both computations (early-exit block line 332 and line 393) become:

```python
        need_parent = parent is not None or (
            len(issues) >= 2 and any(i.get("number") is None for i in issues)
        )
```

Update the comment above the second one: an adopted parent attaches everything (even a single new issue); a new parent is only created for ≥ 2 issues when none exists. The "pre-feature runs stay flat" note still holds when no parent exists anywhere.

3. **Union in the completion callback** — in `run_ticket_issues`, replace `payload = _issues_payload(issues)` (line 214) and the `issues=payload` argument with:

```python
    # Odoo's issues-created handler REPLACES the record's issue list with this
    # payload — send the union across all of this record's runs so earlier
    # requests' issues survive (wizard + planner requests accumulate).
    union = writers.get_ticket_issue_union(
        ctx.db, params.odoo_instance_id, params.ticket_id, params.model_name
    )
```

…callback gets `issues=union`; the final log/return use `issues=len(union)`. Delete `_issues_payload` (now unused — this change orphaned it).

4. **Union in state sync** — in `sync_ticket_issue_state`, replace the snapshot comprehension (lines 266-271) with:

```python
        snapshot = writers.get_ticket_issue_union(
            ctx.db, record["odoo_instance_id"], record["ticket_id"], record["model_name"]
        )
```

5. **`reva/odoo_client.py`** — update the `issues_created` docstring (items are `{"number","title","url","state"}` and the full union across the record's runs) and the `issue_state` docstring (snapshot = the same union). No code changes.

- [ ] **Step 4: Run the whole worker suite** — `cd worker && .venv/bin/python -m pytest tests/ -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add worker/worker/ticket_issue_runner.py reva/odoo_client.py worker/tests/test_ticket_issue_runner.py
git commit -m "feat(issues): one epic per ticket + union-snapshot Odoo callbacks"
```

---

### Task 7: Surface `issue_type` — API run summary + TUI Tickets detail

**Files:**
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/api/app/schemas/ticket_issues.py:53-68` (`TicketIssueRunSummary`)
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/api/app/queries/ticket_issues.py` (items dict)
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/tui/internal/api/types.go:188-201`
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/tui/internal/ui/tickets.go` (struct fields ~45, enter handler ~342, `detailView` ~604)
- Test: `/home/joseph/Projects/Cloudunify/cu_reva/api/tests/test_v1_ticket_issues.py`, `/home/joseph/Projects/Cloudunify/cu_reva/tui/internal/ui/tickets_test.go`

**Interfaces:**
- Consumes: `ticket_issue_runs.issue_type` (Task 1).
- Produces: `TicketIssueRunSummary.issue_type: str | None` (JSON `issue_type`); Go `TicketIssueRunSummary.IssueType *string`; detail header shows `· type CR` for typed runs.

- [ ] **Step 1: Failing API test:**

```python
def test_run_list_exposes_issue_type(client_db_queue):
    tc, db, queue, headers = client_db_queue
    tc.post("/api/v1/create-issues", json={**CONTRACT_PAYLOAD, "issue_type": "CR"}, headers=headers)
    items = tc.get("/api/v1/ticket-issue-runs").json()["items"]
    assert items[0]["issue_type"] == "CR"
```

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_ticket_issues.py -k exposes_issue_type -v` → FAIL (KeyError).

- [ ] **Step 2: Implement REVA side.** `TicketIssueRunSummary`: add `issue_type: str | None = None` after `status`. `api/app/queries/ticket_issues.py`: add `"issue_type": r.issue_type,` after `"status": r.status,`. Re-run → PASS.

- [ ] **Step 3: TUI.** `types.go` `TicketIssueRunSummary`: add after `Status`:

```go
	IssueType        *string          `json:"issue_type"`
```

`tickets.go`: add field `detailIssueType string` next to `detailIssues` (line 45); in the enter handler (after line 343):

```go
				t.detailIssueType = ""
				if cur.row.issueRun.IssueType != nil {
					t.detailIssueType = *cur.row.issueRun.IssueType
				}
```

In `detailView` (line 604), build the header label in a variable and append the tag:

```go
	label := fmt.Sprintf("GitHub Issues  (%d created / %d planned)", created, len(t.detailIssues))
	if t.detailIssueType != "" {
		label += "  · type " + t.detailIssueType
	}
	header := styleTitle.Padding(0, 1).Render(label)
```

Add to `tickets_test.go` (match its existing test style):

```go
func TestDetailViewShowsIssueType(t *testing.T) {
	tt := Tickets{detail: true, detailIssueType: "CR",
		detailIssues: []api.TicketIssueRef{{Title: "x"}}}
	if out := tt.detailView(80, 20); !strings.Contains(out, "type CR") {
		t.Errorf("detail header missing type tag:\n%s", out)
	}
}
```

- [ ] **Step 4: Gate** — `cd tui && go build ./... && go vet ./... && go test ./...` → PASS.

- [ ] **Step 5: Commit**

```bash
git add api/app/schemas/ticket_issues.py api/app/queries/ticket_issues.py api/tests/test_v1_ticket_issues.py tui/internal/api/types.go tui/internal/ui/tickets.go tui/internal/ui/tickets_test.go
git commit -m "feat(issues): expose issue_type in run feed + TUI Tickets detail"
```

---

### Task 8: cu_reva docs + full test gate

**Files:**
- Modify: `/home/joseph/Projects/Cloudunify/cu_reva/docs/github-issue-creation.md`

- [ ] **Step 1: Update the doc.** Precise claims to correct/add (verify line positions against the file):
  - Contract 1 field table: add optional `issue_type` (8 codes, "" treated as unset; wizard flow).
  - Title format line (~docs:83): now `[TYPE] <ticket_id> - <tldr ≤30 chars> (n/total)`; `(n/total)` omitted for single-issue requests; parent = `[TYPE] <ticket_id> - <name tldr>` with dominant child type.
  - Labels: `reva-ticket` + the type code.
  - Contract 2: payload items now `{number, title, url, state}` and are the **union** across the record's runs (replace-safe); same union for Contract 3 snapshots.
  - Epic semantics: one parent per ticket+repo, adopted across runs.
  - Marker section (~docs:74): fix the stale description — the digest also folds in `planning_basis` (and typed requests prefix the basis with the lowercased type code); mention the separate `revaticketparent` marker.
- [ ] **Step 2: Full gate** (shared `reva/` was touched → all three services):

```bash
make test
ruff check reva worker/worker api/app scheduler/scheduler
cd tui && go build ./... && go vet ./... && go test ./...
```

Expected: all green. (Migration 023's raw SQL is Postgres-only — validated by `make test-integration` or first staging boot; state this in the summary honestly.)

- [ ] **Step 3: Commit**

```bash
git add docs/github-issue-creation.md
git commit -m "docs(issues): typed requests, title convention, union callbacks, epic semantics"
```

---

### Task 9 (ast-odoo): callback semantics — `state` passthrough, failed keeps records, list visibility

Repo: `/home/joseph/Projects/Cloudunify/ast-odoo` (separate git repo). Addon: `custom_addons/cu_reva_ticket_analysis`.

**Files:**
- Modify: `routers/reva_router.py:32-35` (`IssueItem`)
- Modify: `models/reva_mixin.py:122-151` (`_apply_reva_issues`)
- Modify: `views/helpdesk_ticket_views.xml:89` and `views/project_task_views.xml` (same element)
- Test: `tests/test_mixin.py` (and existing `tests/test_callback.py` stays green)

**Interfaces:**
- Consumes: REVA's union payload with per-item `state`.
- Produces: `_apply_reva_issues("created", items)` honors `item["state"]`; `_apply_reva_issues("failed", ...)` no longer unlinks; issue list visible whenever records exist.

- [ ] **Step 1: Failing tests** — add to the class in `tests/test_mixin.py` that exercises `_apply_reva_issues` (reuse its setUp; it creates a ticket):

```python
    def test_apply_issues_honours_state(self):
        self.ticket._apply_reva_issues(
            "created",
            [{"number": 1, "title": "a", "url": "https://gh/1", "state": "closed"},
             {"number": 2, "title": "b", "url": "https://gh/2", "state": "open"}],
        )
        by_number = {i.number: i.state for i in self.ticket.reva_issue_ids}
        self.assertEqual(by_number, {1: "closed", 2: "open"})

    def test_apply_issues_failed_keeps_existing_records(self):
        self.env["reva.github.issue"].create(
            {"helpdesk_ticket_id": self.ticket.id, "number": 7,
             "title": "keep me", "url": "https://gh/7", "state": "open"}
        )
        self.ticket._apply_reva_issues("failed", [], "boom")
        self.assertEqual(self.ticket.reva_issue_status, "failed")
        self.assertEqual(len(self.ticket.reva_issue_ids), 1)
```

- [ ] **Step 2: Verify failure** — run the addon suite (`--test-tags cu_reva`, command in Global Constraints) → the two new tests FAIL (`state` KeyError-ish / records unlinked).

- [ ] **Step 3: Implement.**

`routers/reva_router.py` `IssueItem`:

```python
class IssueItem(BaseModel):
    number: int
    title: str
    url: str
    # REVA sends the union of all runs incl. previously-closed issues; the
    # default keeps compatibility with a pre-union REVA that omits the field.
    state: Literal["open", "closed"] = "open"
```

`models/reva_mixin.py` `_apply_reva_issues`:
- failed branch: **delete** the `self.reva_issue_ids.unlink()` line (line 125) — a failed follow-up request must not wipe issues earlier requests created.
- created branch: `"state": issue.get("state", "open"),` instead of `"state": "open",` (line 140). Update the "Replace the set" comment: the payload is REVA's union across all of the record's requests, so replace == complete list.

Both view files: change line 89 (helpdesk) / the matching line (project_task):

```xml
<field name="reva_issue_ids" readonly="1" invisible="not reva_issue_ids">
```

(keeps the list visible while a follow-up request is pending/failed).

- [ ] **Step 4: Run the addon suite** → PASS (all, incl. `test_callback.py` — its payloads omit `state`, covered by the default).

- [ ] **Step 5: Commit** (in ast-odoo, match its commit style)

```bash
git add custom_addons/cu_reva_ticket_analysis
git commit -m "[IMP] cu_reva_ticket_analysis: issue state passthrough, failed callback keeps issue list"
```

---

### Task 10 (ast-odoo): wizard + shared send path + views/security/manifest

**Files:**
- Create: `custom_addons/cu_reva_ticket_analysis/wizard/__init__.py`, `wizard/reva_issue_wizard.py`, `views/reva_issue_wizard_views.xml`
- Modify: `__init__.py` (root), `__manifest__.py` (version `19.0.9.0.0`, data list), `models/reva_mixin.py` (refactor + 2 new actions), `views/helpdesk_ticket_views.xml`, `views/project_task_views.xml` (button), `security/ir.model.access.csv`
- Test: `tests/test_action.py`

**Interfaces:**
- Consumes: REVA's optional `issue_type` (Task 3), `_reva_headers`/`_reva_error_message` (existing).
- Produces: `record._send_issue_request(description, analysis_html="", issue_type=None, extra=None)`; `record.action_open_issue_wizard()`; TransientModel `reva.issue.wizard` with `action_send()`.

- [ ] **Step 1: Failing tests** — extend the create-issues test class in `tests/test_action.py` (the class exercising `action_create_github_issues`; reuse its setUp — a ticket whose project has `reva_github_url` + `reva_enabled` — and its 202-mock helper, which returns `{"request_id": ...}`):

```python
    def test_wizard_sends_typed_request(self):
        wizard = self.env["reva.issue.wizard"].create({
            "res_model": "helpdesk.ticket",
            "res_id": self.ticket.id,
            "issue_type": "CR",
            "text": "Please change the delivery slip layout",
        })
        with patch(
            "odoo.addons.cu_reva_ticket_analysis.models.reva_mixin.requests.post",
            return_value=self._mock_202(),
        ) as post:
            wizard.action_send()
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["issue_type"], "CR")
        self.assertEqual(payload["description"], "Please change the delivery slip layout")
        self.assertEqual(payload["analysis_html"], "")
        self.assertNotIn("description_docx", payload)
        self.assertEqual(self.ticket.reva_issue_status, "pending")
        self.assertTrue(self.ticket.reva_issue_request_id)

    def test_wizard_untyped_omits_issue_type(self):
        wizard = self.env["reva.issue.wizard"].create({
            "res_model": "helpdesk.ticket",
            "res_id": self.ticket.id,
            "text": "Something is off",
        })
        with patch(
            "odoo.addons.cu_reva_ticket_analysis.models.reva_mixin.requests.post",
            return_value=self._mock_202(),
        ) as post:
            wizard.action_send()
        self.assertNotIn("issue_type", post.call_args.kwargs["json"])
```

(If that class's 202-mock helper has a different name/shape, reuse it as-is — it must return `{"request_id": <int>}`.)

- [ ] **Step 2: Verify failure** — addon suite → FAIL (unknown model `reva.issue.wizard`).

- [ ] **Step 3: Implement.**

**`models/reva_mixin.py`** — replace `action_create_github_issues` (lines 311-381) with a thin wrapper + shared sender, and add the wizard opener:

```python
    def action_create_github_issues(self):
        self.ensure_one()
        plain_text = html2plaintext(getattr(self, "description", "") or "")
        analysis_html = (
            str(self.reva_analysis)
            if self.reva_status == "completed" and self.reva_analysis
            else ""
        )
        extra = {}
        # REVA accepts a consultant attachment (.docx/.pdf/.txt) for create-issues on tasks only.
        if self._name == "project.task":
            attachment = self._reva_attachment_payload()
            if attachment:
                extra["description_docx"] = attachment
        return self._send_issue_request(plain_text, analysis_html=analysis_html, extra=extra)

    def action_open_issue_wizard(self):
        """Open the ad-hoc issue-request wizard (typed CR/BUG/… free text)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Create GitHub Issue",
            "res_model": "reva.issue.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_res_model": self._name,
                "default_res_id": self.id,
            },
        }

    def _send_issue_request(self, description, analysis_html="", issue_type=None, extra=None):
        """POST a create-issues request to REVA and set the pending state.

        Shared by the analysis-based button (description = ticket text, may
        carry the consultant file) and the wizard (description = the typed
        request text, optional fixed issue_type).
        """
        self.ensure_one()
        # sudo: reva.url and reva.api_key are admin-only params; key is used server-side only, never returned to the user
        ICP = self.env["ir.config_parameter"].sudo()
        reva_url = ICP.get_param("reva.url", "")
        if not reva_url:
            raise UserError("REVA is not configured. Please set the REVA API URL in Settings → Technical → REVA.")
        if not self.reva_github_url:
            raise UserError(
                "No GitHub Project URL is set on this record's project. Set it on the project form, Settings tab."
            )
        headers = self._reva_headers()
        ticket_url = f"{self.get_base_url()}/web#id={self.id}&model={self._name}&view_type=form"

        # Set pending before the call. Any UserError below rolls back the
        # transaction, so the status reverts on error — "failed" is only ever
        # set by the REVA callback, which runs in its own committed request.
        self.write({"reva_issue_status": "pending"})

        payload = {
            "ticket_id": self.id,
            "model_name": self._name,
            "github_url": self.reva_github_url,
            "name": self.name,
            "description": description,
            "analysis_html": analysis_html,
            "priority": getattr(self, "priority", "") or "0",
            "ticket_url": ticket_url,
        }
        if issue_type:
            payload["issue_type"] = issue_type
        payload.update(extra or {})

        try:
            resp = requests.post(
                f"{reva_url.rstrip('/')}/api/v1/create-issues",
                json=payload,
                headers=headers,
                timeout=10,
            )
        except requests.exceptions.Timeout:
            raise UserError("REVA did not respond in time. Please try again in a moment.")
        except requests.exceptions.ConnectionError:
            raise UserError("Could not reach REVA. Check that the REVA API URL is correct and the service is running.")

        if resp.status_code != 202:
            raise UserError(self._reva_error_message(resp))

        try:
            data = resp.json()
            request_id = data["request_id"]
        except Exception:
            raise UserError("REVA returned an unexpected response format. Please contact your administrator.")

        self.write({"reva_issue_request_id": request_id})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "message": "Request sent to REVA — GitHub issues will appear here when ready.",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }
```

**`wizard/reva_issue_wizard.py`:**

```python
# Copyright (c) 2026 cloudunify FlexCo
# Author: cloudunify FlexCo
# OPL-1
"""Ad-hoc GitHub issue request wizard: typed free text sent to REVA."""

from odoo import fields, models
from odoo.exceptions import UserError

ISSUE_TYPE_SELECTION = [
    ("BUG", "Bug"),
    ("FEAT", "Feature"),
    ("CR", "Change Request"),
    ("CONF", "Configuration"),
    ("DEV", "Development"),
    ("MIG", "Migration"),
    ("SUP", "Support"),
    ("DOC", "Documentation"),
]


class RevaIssueWizard(models.TransientModel):
    _name = "reva.issue.wizard"
    _description = "REVA GitHub Issue Request"

    # Selection (not Char) so a crafted context can't point the wizard at an
    # arbitrary model — mirrors the router's _ALLOWED_MODELS.
    res_model = fields.Selection(
        selection=[("helpdesk.ticket", "Helpdesk Ticket"), ("project.task", "Project Task")],
        required=True,
    )
    res_id = fields.Integer(required=True)
    issue_type = fields.Selection(
        selection=ISSUE_TYPE_SELECTION,
        string="Type",
        help="Optional. Leave empty and REVA picks the type from the text.",
    )
    text = fields.Text(
        string="Request",
        required=True,
        help="Describe the change, bug, or task. REVA turns it into one or "
        "more GitHub issues (long texts are split).",
    )

    def action_send(self):
        self.ensure_one()
        record = self.env[self.res_model].browse(self.res_id)
        if not record.exists():
            raise UserError("The record this wizard was opened from no longer exists.")
        return record._send_issue_request(self.text, issue_type=self.issue_type or None)
```

**`wizard/__init__.py`:**

```python
# Copyright (c) 2026 cloudunify FlexCo
# Author: cloudunify FlexCo
# OPL-1
# pylint: disable=missing-module-docstring
from . import reva_issue_wizard
```

Root `__init__.py`: `from . import models, routers, wizard`

**`views/reva_issue_wizard_views.xml`:**

```xml
<?xml version="1.0" encoding="UTF-8" ?>
<odoo>
    <record id="reva_issue_wizard_view_form" model="ir.ui.view">
        <field name="name">reva.issue.wizard.form</field>
        <field name="model">reva.issue.wizard</field>
        <field name="arch" type="xml">
            <form string="Create GitHub Issue">
                <field name="res_model" invisible="1" />
                <field name="res_id" invisible="1" />
                <group>
                    <field name="issue_type" placeholder="Let REVA decide" />
                    <field name="text" placeholder="Describe the change, bug, or task…" />
                </group>
                <footer>
                    <button string="Send to REVA" name="action_send" type="object" class="btn-primary" />
                    <button string="Cancel" special="cancel" class="btn-secondary" />
                </footer>
            </form>
        </field>
    </record>
</odoo>
```

**Both ticket view files** — add after the "Create Issues Again" button:

```xml
                    <button
                        name="action_open_issue_wizard"
                        type="object"
                        string="Create Issue"
                        class="btn-secondary mb8"
                        invisible="reva_issue_status == 'pending' or not reva_github_url"
                    />
```

**`security/ir.model.access.csv`** — append:

```csv
access_reva_issue_wizard_user,reva.issue.wizard.user,model_reva_issue_wizard,base.group_user,1,1,1,1
```

**`__manifest__.py`** — `"version": "19.0.9.0.0"`, and add `"views/reva_issue_wizard_views.xml"` to `data` (after the ticket views).

- [ ] **Step 4: Run the addon suite** (with `-u cu_reva_ticket_analysis` so the new view/security files load) → PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_addons/cu_reva_ticket_analysis
git commit -m "[ADD] cu_reva_ticket_analysis: ad-hoc typed issue-request wizard (19.0.9.0.0)"
```

---

### Task 11 (ast-odoo): handoff doc + final gates

**Files:**
- Modify: `custom_addons/cu_reva_ticket_analysis/docs/github-issues-handoff.md`, `README.md` (if it documents the buttons)

- [ ] **Step 1: Update the handoff doc:** Contract 1 gains optional `issue_type` (8 codes); Contract 2 items gain `state` and the payload is the union of all the record's runs (so replace semantics stay correct); the failed callback no longer clears the issue list; new wizard flow description; version note 19.0.9.0.0.
- [ ] **Step 2: Full addon test run** (`--test-tags cu_reva`) → PASS. Commit:

```bash
git add custom_addons/cu_reva_ticket_analysis
git commit -m "[IMP] cu_reva_ticket_analysis: document typed issue requests + union callback semantics"
```

- [ ] **Step 3 (cu_reva): honest closing summary.** State what is unit-tested vs not: migration 023 raw SQL and the Postgres-only paths need `make test-integration`/staging; the live Claude planner + real GitHub/Odoo round-trip is untested until a smoke test against a throwaway repo. Deploy REVA first, then upgrade the addon (`-u cu_reva_ticket_analysis`).
