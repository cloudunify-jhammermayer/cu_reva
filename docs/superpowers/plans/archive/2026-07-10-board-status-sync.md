# Board Status Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GitHub Projects board Status follows the PR lifecycle for REVA-created issues — linked PR opened/reopened/ready → "In Progress", REVA review completed → "In review" — covering exactly the legs GitHub's native workflows cannot do.

**Spec:** `docs/superpowers/specs/2026-07-10-board-status-sync-design.md` (approved 2026-07-10).

**Architecture:** One new no-Claude RQ job (`run_board_status_update`) fed from two triggers: the `pull_request` webhook (api) and a post-posting hook in `run_review` (worker — `WorkerContext` gains an optional `rq_queue` so the worker can enqueue). The job resolves the PR's closing refs (body regex, falling back to the GraphQL `closingIssuesReferences` union when the body yields no matches), looks up matching open REVA issues carrying a persisted `project_item_id` via a new writer query, checks the repo kill switch, and sets the board's existing Status option. Fail-soft everywhere (the board is a bonus); every degradation logs + ops-events under `component="board_status"`.

**Planning deviation from the spec, with reason:** the spec suggested extracting the runner's `_board_context` helper into a shared module. That helper *creates and renames fields* (Due date/Estimate/Priority) — behavior this feature must never perform ("existing options only, never create"). Sharing it would couple two flows that must differ, so the job gets its own ~20-line read-only Status resolver instead; the genuinely shared pieces (`parse_github_project_url`, `get_project`, `set_project_item_option`) already live in `reva/`. Task 5 records this in the spec.

**Tech Stack:** Python 3.14, RQ, GraphQL via the existing `GitHubClient`, SQLAlchemy. No DB migrations, no Claude calls, no TUI surface (state lives on the GitHub board; failures surface via ops events → Failures tab, as the spec documents).

## Global Constraints

- REVA **never sets "Todo" or "Done"** (native-workflow domain) and **never creates Status options or fields** — a missing "In Progress"/"In review" option is configuration, not degradation: debug-log + skip, no ops event.
- Triggers → option names: `pr_active` → `In Progress`; `review_done` → `In review` (case-insensitive name match against existing options).
- Only REVA-created issues move (they alone have a persisted `project_item_id` in `ticket_issue_runs.issues` JSON); closed issues are skipped; no backward moves.
- Kill switch `RepoConfig.board_status_sync: bool = True` (`.claude-review.yml`), checked **in the job** (webhook handler stays thin), fail-open on config-fetch/parse errors (mirrors `_change_notes_enabled` semantics).
- Failure posture: `TransientError` → re-raise (RQ retry; the job is idempotent — re-setting the same option is a no-op). Permanent/unexpected errors → swallow + log + `record_ops_event(component="board_status", ...)`. Never fails a review, a webhook, or an Odoo callback.
- Enqueue retry policy matches the house pattern: `retry=Retry(max=3, interval=[30, 120, 300])`.
- `reva/` and `worker/`+`api/` are touched: final verification is `make test`, `ruff check reva worker/worker api/app scheduler/scheduler`, `cd tui && go build ./... && go vet ./... && go test ./...` (TUI untouched — build/vet/test must simply stay green).
- Per-service venvs: `cd worker && .venv/bin/python -m pytest tests/...`, `cd api && .venv/bin/python -m pytest tests/...`.

---

### Task 1: Kill switch + board-items writer query

**Files:**
- Modify: `reva/types.py` (`RepoConfig`, after `intent_check`)
- Modify: `reva/db/writers.py` (new function after `get_ticket_issue_union`, ~line 2172)
- Test: `worker/tests/test_review_tool.py` (config default), `worker/tests/test_ticket_issue_writers.py` (query matrix)

**Interfaces:**
- Produces: `RepoConfig.board_status_sync: bool = True`; `writers.get_board_items_for_issues(db, repo_full_name: str, issue_numbers: list[int]) -> list[dict]` returning `[{"number": int, "project_item_id": str, "github_project_url": str}]` for open, board-placed REVA issues among `issue_numbers` (newest-run occurrence decides state; skips closed items, items without `project_item_id`, runs without `github_project_url`). Task 2 consumes both.

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_review_tool.py`:

```python
def test_repo_config_board_status_sync_defaults_on():
    assert RepoConfig().board_status_sync is True
    assert RepoConfig.model_validate({"board_status_sync": False}).board_status_sync is False
```

Append to `worker/tests/test_ticket_issue_writers.py` (uses the file's existing `_complete_run`/`_typed_params` helpers; `_typed_params` accepts overrides — check its signature and pass the project URL the way the file's project-related tests do; if none exists, set `github_project_url` on the run row directly via a session update after `_complete_run`, matching the ORM field name):

```python
def test_board_items_matrix(db):
    url = "https://github.com/orgs/acme/projects/7"
    _complete_run(db, _typed_params(ticket_id=97), [
        {"number": 50, "title": "open+board", "url": "https://gh/50", "state": "open",
         "project_item_id": "PVTI_50"},
        {"number": 51, "title": "closed", "url": "https://gh/51", "state": "closed",
         "project_item_id": "PVTI_51"},
        {"number": 52, "title": "no item id", "url": "https://gh/52", "state": "open"},
    ])
    # Stamp the board URL on the run (the helper may not set it).
    with db.session() as s:
        from reva.db.models import TicketIssueRun
        row = s.query(TicketIssueRun).filter_by(ticket_id=97).one()
        row.github_project_url = url

    items = writers.get_board_items_for_issues(db, "acme/widgets", [50, 51, 52, 99])
    assert items == [
        {"number": 50, "project_item_id": "PVTI_50", "github_project_url": url}
    ]


def test_board_items_empty_for_no_refs_or_no_board(db):
    assert writers.get_board_items_for_issues(db, "acme/widgets", []) == []
    # Run without a github_project_url never yields items.
    _complete_run(db, _typed_params(ticket_id=98), [
        {"number": 60, "title": "boardless", "url": "https://gh/60", "state": "open",
         "project_item_id": "PVTI_60"},
    ])
    assert writers.get_board_items_for_issues(db, "acme/widgets", [60]) == []


def test_board_items_newest_run_occurrence_decides(db):
    url = "https://github.com/orgs/acme/projects/7"
    p = _typed_params(ticket_id=99)
    _complete_run(db, p, [
        {"number": 70, "title": "old open", "url": "https://gh/70", "state": "open",
         "project_item_id": "PVTI_70"},
    ])
    _complete_run(db, p, [
        {"number": 70, "title": "now closed", "url": "https://gh/70", "state": "closed",
         "project_item_id": "PVTI_70"},
    ])
    with db.session() as s:
        from reva.db.models import TicketIssueRun
        for row in s.query(TicketIssueRun).filter_by(ticket_id=99).all():
            row.github_project_url = url
    # Newest occurrence says closed -> the older open duplicate must NOT resurface.
    assert writers.get_board_items_for_issues(db, "acme/widgets", [70]) == []
```

(Adjust the repo name in the assertions to whatever `_typed_params` seeds as `repo_full_name` — read the helper first; the matrix logic is the requirement.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_writers.py -k board_items -v tests/test_review_tool.py -k board_status -v`
Expected: FAIL — `AttributeError: module 'reva.db.writers' has no attribute 'get_board_items_for_issues'`; `RepoConfig` has no `board_status_sync`.

- [ ] **Step 3: Implement**

`reva/types.py`, in `RepoConfig` after the `intent_check` field:

```python
    # Kill switch for GitHub Projects board Status sync (linked-PR legs):
    # false stops REVA moving cards to "In Progress"/"In review" for this repo.
    board_status_sync: bool = True
```

`reva/db/writers.py`, after `get_ticket_issue_union`:

```python
def get_board_items_for_issues(
    db: Database, repo_full_name: str, issue_numbers: list[int]
) -> list[dict]:
    """Open REVA-created issues among `issue_numbers` that sit on a Projects
    board: [{number, project_item_id, github_project_url}]. The newest run's
    occurrence of a number decides (mirrors get_ticket_issue_union's
    newest-wins dedup) — a closed newest occurrence is skipped even if an
    older run still shows it open. Runs without a board URL and items without
    a persisted project_item_id never match (board-status spec 2026-07-10)."""
    if not issue_numbers:
        return []
    wanted = set(issue_numbers)
    repo = repo_full_name.lower()
    out: dict[int, dict] = {}
    seen: set[int] = set()
    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.repo_full_name == repo,
                TicketIssueRun.issues.is_not(None),
                TicketIssueRun.github_project_url.is_not(None),
            )
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
        ).scalars().all()
        for row in rows:
            for item in row.issues or []:
                n = item.get("number")
                if n is None or n not in wanted or n in seen:
                    continue
                seen.add(n)  # newest occurrence decides, even when skipped
                if item.get("state") == "closed" or not item.get("project_item_id"):
                    continue
                out[n] = {
                    "number": n,
                    "project_item_id": item["project_item_id"],
                    "github_project_url": row.github_project_url,
                }
    return sorted(out.values(), key=lambda i: i["number"])
```

- [ ] **Step 4: Run the covering suites**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_writers.py tests/test_review_tool.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add reva/types.py reva/db/writers.py worker/tests/test_review_tool.py worker/tests/test_ticket_issue_writers.py
git commit -m "feat(board): board_status_sync kill switch + board-items lookup for linked issues"
```

---

### Task 2: The board-status job

**Files:**
- Create: `worker/worker/board_status_runner.py`
- Create: `worker/worker/board_status_tasks.py`
- Test: `worker/tests/test_board_status_runner.py`

**Interfaces:**
- Consumes: `writers.get_board_items_for_issues`, `RepoConfig.board_status_sync` (Task 1); existing `reva.ticket_links.parse_closing_refs`, `reva.github_urls.parse_github_project_url`, `GitHubClient.get_pull_request / get_file_content / get_closing_issue_numbers / get_project / set_project_item_option`, `worker.runner.get_context`, `worker.task_contract.terminal_on_permanent`.
- Produces: RQ entry `worker.board_status_tasks.run_board_status_update` with params `{"repo_full_name", "pr_number", "installation_id", "trigger": "pr_active"|"review_done"}`. Tasks 3 and 4 enqueue exactly this.

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_board_status_runner.py` (mirrors `test_comment_reply.py`'s MagicMock-context pattern):

```python
"""Board-status job tests — fakes only, no network."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from reva.db import writers
from reva.db.engine import Database, create_engine_from_url
from reva.db.models import Base, TicketIssueRun
from reva.errors import PermanentError, TransientError
from worker.board_status_runner import run_board_status_update
from worker.runner import WorkerContext, set_context

_URL = "https://github.com/orgs/acme/projects/7"
_PROJECT = {
    "id": "PVT_1",
    "fields": [
        {"id": "F_STATUS", "name": "Status", "dataType": "SINGLE_SELECT",
         "options": [
             {"id": "OPT_TODO", "name": "Todo"},
             {"id": "OPT_PROG", "name": "In Progress"},
             {"id": "OPT_REV", "name": "In review"},
             {"id": "OPT_DONE", "name": "Done"},
         ]},
    ],
}


@pytest.fixture()
def db():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _seed_board_issue(db, *, number=50, state="open", item_id="PVTI_50",
                      url=_URL, ticket_id=97):
    with db.session() as s:
        s.add(TicketIssueRun(
            odoo_instance_id=1, ticket_id=ticket_id, model_name="helpdesk.ticket",
            repo_full_name="acme/widgets", status="completed",
            github_project_url=url,
            issues=[{"number": number, "title": "t", "url": f"https://gh/{number}",
                     "state": state, "project_item_id": item_id}],
        ))


def _ctx(db, *, pr_body="Closes #50", config_yaml=None, closing_numbers=None,
         project=_PROJECT):
    github = MagicMock()
    github.get_installation_token.return_value = "tok"
    github.get_pull_request.return_value = {"body": pr_body, "head": {"sha": "abc"}}
    github.get_file_content.return_value = config_yaml
    github.get_closing_issue_numbers.return_value = closing_numbers or []
    github.get_project.return_value = project
    ctx = WorkerContext(
        db=db, claude=MagicMock(), runner=None, github=github,  # type: ignore[arg-type]
        reviewer=None, auditor=None, ticket_analyzer=None, verifier=None,  # type: ignore[arg-type]
    )
    set_context(ctx)
    return ctx


def _params(trigger="pr_active"):
    return {"repo_full_name": "acme/widgets", "pr_number": 42,
            "installation_id": 99, "trigger": trigger}


def test_pr_active_sets_in_progress(db):
    _seed_board_issue(db)
    ctx = _ctx(db)
    out = run_board_status_update(_params("pr_active"))
    assert out == {"status": "completed", "moved": 1}
    ctx.github.set_project_item_option.assert_called_once_with(
        "tok", "PVT_1", "PVTI_50", "F_STATUS", "OPT_PROG")


def test_review_done_sets_in_review(db):
    _seed_board_issue(db)
    ctx = _ctx(db)
    run_board_status_update(_params("review_done"))
    ctx.github.set_project_item_option.assert_called_once_with(
        "tok", "PVT_1", "PVTI_50", "F_STATUS", "OPT_REV")


def test_sidebar_only_link_found_via_graphql_fallback(db):
    _seed_board_issue(db)
    ctx = _ctx(db, pr_body="no closing keywords", closing_numbers=[50])
    out = run_board_status_update(_params())
    assert out["moved"] == 1
    ctx.github.get_closing_issue_numbers.assert_called_once()


def test_no_refs_anywhere_is_noop_without_board_calls(db):
    _seed_board_issue(db)
    ctx = _ctx(db, pr_body="plain refactor", closing_numbers=[])
    out = run_board_status_update(_params())
    assert out == {"status": "no_board_items"}
    ctx.github.get_project.assert_not_called()
    ctx.github.get_file_content.assert_not_called()  # config only fetched when items exist


def test_kill_switch_disables(db):
    _seed_board_issue(db)
    ctx = _ctx(db, config_yaml="board_status_sync: false\n")
    out = run_board_status_update(_params())
    assert out == {"status": "disabled"}
    ctx.github.set_project_item_option.assert_not_called()


def test_config_parse_error_fails_open(db):
    _seed_board_issue(db)
    ctx = _ctx(db, config_yaml=":: not yaml ::[")
    out = run_board_status_update(_params())
    assert out["moved"] == 1


def test_missing_option_is_silent_skip_no_ops_event(db):
    project = {"id": "PVT_1", "fields": [
        {"id": "F_STATUS", "name": "Status", "dataType": "SINGLE_SELECT",
         "options": [{"id": "OPT_TODO", "name": "Todo"}]}]}
    _seed_board_issue(db)
    ctx = _ctx(db, project=project)
    out = run_board_status_update(_params())
    assert out == {"status": "completed", "moved": 0}
    ctx.github.set_project_item_option.assert_not_called()
    assert writers.list_ops_events(db, limit=10) == []  # config, not degradation


def test_transient_set_failure_reraises_for_rq_retry(db):
    _seed_board_issue(db)
    ctx = _ctx(db)
    ctx.github.set_project_item_option.side_effect = TransientError("503")
    with pytest.raises(TransientError):
        run_board_status_update(_params())


def test_permanent_set_failure_swallowed_with_ops_event(db):
    _seed_board_issue(db)
    ctx = _ctx(db)
    ctx.github.set_project_item_option.side_effect = PermanentError("422")
    out = run_board_status_update(_params())
    assert out == {"status": "completed", "moved": 0}
    events = writers.list_ops_events(db, limit=10)
    assert any(e["event"] == "set_option_failed" for e in events)


def test_graphql_link_lookup_failure_degrades_with_ops_event(db):
    _seed_board_issue(db)
    ctx = _ctx(db, pr_body="no refs")
    ctx.github.get_closing_issue_numbers.side_effect = PermanentError("boom")
    out = run_board_status_update(_params())
    assert out == {"status": "no_board_items"}
    events = writers.list_ops_events(db, limit=10)
    assert any(e["event"] == "link_resolution_failed" for e in events)


def test_one_get_project_per_board_for_multiple_items(db):
    _seed_board_issue(db, number=50, item_id="PVTI_50", ticket_id=97)
    _seed_board_issue(db, number=51, item_id="PVTI_51", ticket_id=98)
    ctx = _ctx(db, pr_body="Closes #50 fixes #51")
    out = run_board_status_update(_params())
    assert out["moved"] == 2
    assert ctx.github.get_project.call_count == 1
```

If `writers.list_ops_events` doesn't exist under that name, use the reader the ops-events API route uses (grep `record_ops_event`'s table + its reader in `reva/db/writers.py`) — the assertion intent is "an ops event row with that event name exists / none exists".

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_board_status_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'worker.board_status_runner'`

- [ ] **Step 3: Implement the runner**

Create `worker/worker/board_status_runner.py`:

```python
"""GitHub Projects board Status sync — the linked-PR legs native workflows
can't express (board-status spec 2026-07-10).

Native project workflows own added->Todo and closed/merged->Done; this job
owns only: linked PR active -> "In Progress", REVA review completed ->
"In review". Existing options only — never creates fields or options, never
touches Todo/Done. Fail-soft by design: the board is a bonus; every
degradation logs AND records an ops event (component="board_status")."""

from __future__ import annotations

from collections import defaultdict

import structlog
import yaml

from reva.db import writers
from reva.errors import TransientError
from reva.github_urls import parse_github_project_url
from reva.ticket_links import parse_closing_refs
from reva.types import RepoConfig
from worker.runner import get_context

logger = structlog.get_logger()

_OPTION_BY_TRIGGER = {"pr_active": "In Progress", "review_done": "In review"}


def run_board_status_update(job_params: dict) -> dict:
    ctx = get_context()
    repo = job_params["repo_full_name"].lower()
    pr_number = job_params["pr_number"]
    trigger = job_params["trigger"]
    option_name = _OPTION_BY_TRIGGER.get(trigger)
    if option_name is None:
        return {"status": "unknown_trigger"}
    owner, name = repo.split("/", 1)
    log = logger.bind(repo=repo, pr=pr_number, trigger=trigger)

    token = ctx.github.get_installation_token(job_params["installation_id"])
    pr = ctx.github.get_pull_request(token, owner, name, pr_number)
    refs = parse_closing_refs(pr.get("body"))
    # Full union with GitHub's authoritative link set (sidebar links have no
    # body keyword — and they can coexist with body refs, so this is not a
    # fallback). Degrades to body refs visibly, like the reviewer path.
    try:
        for n in ctx.github.get_closing_issue_numbers(token, owner, name, pr_number):
            if n not in refs:
                refs.append(n)
    except TransientError:
        raise
    except Exception as exc:  # noqa: BLE001 — degrade, stay visible
        log.warning("board_status_link_resolution_failed", exc_info=True)
        writers.record_ops_event(
            ctx.db, "board_status", "warning", "link_resolution_failed",
            {"repo": repo, "pr": pr_number, "error": str(exc)[:300]},
        )
    items = writers.get_board_items_for_issues(ctx.db, repo, refs)
    if not items:
        return {"status": "no_board_items"}

    if not _sync_enabled(ctx, token, owner, name, pr, log):
        return {"status": "disabled"}

    by_board: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        by_board[item["github_project_url"]].append(item)

    moved = 0
    for project_url, board_items in by_board.items():
        resolved = _resolve_status_option(ctx, token, project_url, option_name, log)
        if resolved is None:
            continue
        project_id, field_id, option_id = resolved
        for item in board_items:
            try:
                ctx.github.set_project_item_option(
                    token, project_id, item["project_item_id"], field_id, option_id
                )
                moved += 1
            except TransientError:
                raise  # idempotent job — RQ retries the whole update safely
            except Exception as exc:  # noqa: BLE001
                log.warning("board_status_set_failed", issue=item["number"], exc_info=True)
                writers.record_ops_event(
                    ctx.db, "board_status", "warning", "set_option_failed",
                    {"repo": repo, "pr": pr_number, "issue": item["number"],
                     "error": str(exc)[:300]},
                )
    log.info("board_status_updated", moved=moved, option=option_name)
    return {"status": "completed", "moved": moved}


def _sync_enabled(ctx, token: str, owner: str, name: str, pr: dict, log) -> bool:
    """Per-repo kill switch from .claude-review.yml at the PR head. Fail-open:
    a config hiccup must not silently freeze boards (mirror of the webhook's
    _change_notes_enabled semantics)."""
    try:
        raw = ctx.github.get_file_content(
            token, owner, name, ".claude-review.yml", pr["head"]["sha"]
        )
        if not raw:
            return True
        parsed = yaml.safe_load(raw) or {}
        return RepoConfig.model_validate(parsed).board_status_sync
    except Exception:  # noqa: BLE001
        log.warning("board_status_config_failed", exc_info=True)
        return True


def _resolve_status_option(
    ctx, token: str, project_url: str, option_name: str, log
) -> tuple[str, str, str] | None:
    """(project_id, status_field_id, option_id) — existing options only.

    A board without a Status single-select or without the wanted option is
    CONFIGURATION, not degradation: debug log + skip, no ops event (the
    operator chose their columns). GraphQL errors propagate to the caller's
    fail-soft/transient handling."""
    parsed = parse_github_project_url(project_url or "")
    if parsed is None:
        log.warning("board_status_project_url_invalid", url=project_url)
        writers.record_ops_event(
            ctx.db, "board_status", "warning", "project_url_invalid",
            {"url": (project_url or "")[:200]},
        )
        return None
    owner_type, owner, number = parsed
    try:
        project = ctx.github.get_project(token, owner_type, owner, number)
    except TransientError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("board_status_project_lookup_failed", exc_info=True)
        writers.record_ops_event(
            ctx.db, "board_status", "warning", "project_lookup_failed",
            {"url": project_url[:200], "error": str(exc)[:300]},
        )
        return None
    status_field = next(
        (f for f in project["fields"]
         if f["name"].lower() == "status" and f["dataType"] == "SINGLE_SELECT"),
        None,
    )
    if status_field is None:
        log.debug("board_status_no_status_field", url=project_url)
        return None
    option = next(
        (o for o in status_field.get("options") or []
         if o["name"].lower() == option_name.lower()),
        None,
    )
    if option is None:
        log.debug("board_status_option_missing", wanted=option_name, url=project_url)
        return None
    return project["id"], status_field["id"], option["id"]
```

Create `worker/worker/board_status_tasks.py` (mirrors `change_note_tasks.py`):

```python
"""Stable RQ task entry for board Status sync."""

from worker.board_status_runner import run_board_status_update as _run
from worker.task_contract import terminal_on_permanent

run_board_status_update = terminal_on_permanent(_run)

__all__ = ["run_board_status_update"]
```

- [ ] **Step 4: Run the new suite + neighbors**

Run: `cd worker && .venv/bin/python -m pytest tests/test_board_status_runner.py tests/test_ticket_issue_writers.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add worker/worker/board_status_runner.py worker/worker/board_status_tasks.py worker/tests/test_board_status_runner.py
git commit -m "feat(board): board-status job — PR-active/review-done move linked REVA cards"
```

---

### Task 3: Webhook trigger (api)

**Files:**
- Modify: `api/app/routes/webhooks.py` (`_handle_pull_request`, after the `_upsert_repo_and_pr`/`upsert_pending_review` block for reviewable actions)
- Test: `api/tests/test_webhooks.py`

**Interfaces:**
- Consumes: the Task 2 job path/params verbatim.
- Produces: enqueue on actions `opened`, `reopened`, `ready_for_review` (NOT `synchronize` — a push doesn't change "in progress"-ness and reviews handle the review leg; NOT drafts — the existing draft gate returns before this point, and `ready_for_review` covers the transition).

- [ ] **Step 1: Write the failing tests**

In `api/tests/test_webhooks.py`, mirror the existing change-note enqueue tests (they post a webhook payload with a fake `rq_queue` and assert `enqueue` calls — copy that fixture/idiom exactly):

```python
def test_pr_opened_enqueues_board_status(client_with_queue):
    # opened action → one board-status enqueue with trigger=pr_active
    ...post the standard pull_request 'opened' payload used by neighboring tests...
    calls = [c for c in fake_queue.enqueued
             if c.path == "worker.board_status_tasks.run_board_status_update"]
    assert len(calls) == 1
    assert calls[0].params["trigger"] == "pr_active"
    assert calls[0].params["repo_full_name"] == "acme/widgets"
    assert calls[0].params["pr_number"] == 42
    assert calls[0].params["installation_id"] == 99


def test_pr_synchronize_does_not_enqueue_board_status(client_with_queue):
    ...same payload with action="synchronize"...
    assert not [c for c in fake_queue.enqueued
                if c.path == "worker.board_status_tasks.run_board_status_update"]
```

(The `...` lines mean: reuse the file's actual payload-builder and fake-queue fixtures — read the change-note enqueue tests first and mirror them exactly; the assertions shown are the requirement. Also add `ready_for_review` → enqueues, and draft `opened` → no enqueue, using the same idiom.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && .venv/bin/python -m pytest tests/test_webhooks.py -k board_status -v`
Expected: FAIL — no board-status enqueue happens

- [ ] **Step 3: Implement**

In `_handle_pull_request`, module-level next to `_REVIEWABLE_ACTIONS`:

```python
# Actions that mean "someone is actively working this PR" — the board-status
# job moves linked REVA cards to In Progress. synchronize excluded: pushes
# don't change in-progress-ness (and the review path covers review state).
_BOARD_SYNC_ACTIONS = frozenset({"opened", "reopened", "ready_for_review"})
```

After the `writers.upsert_pending_review(...)` call (still inside the reviewable-action path, after the draft gate):

```python
    if rq_queue is not None and action in _BOARD_SYNC_ACTIONS:
        rq_queue.enqueue(
            "worker.board_status_tasks.run_board_status_update",
            {
                "repo_full_name": repo_data["full_name"].lower(),
                "pr_number": pr_data["number"],
                "installation_id": installation_id,
                "trigger": "pr_active",
            },
            retry=Retry(max=3, interval=[30, 120, 300]),
        )
```

(`Retry` is already imported for the change-note enqueue.) No closing-ref gate here: sidebar-only links have no body refs — the job resolves and no-ops cheaply.

- [ ] **Step 4: Run the webhook suite**

Run: `cd api && .venv/bin/python -m pytest tests/test_webhooks.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add api/app/routes/webhooks.py api/tests/test_webhooks.py
git commit -m "feat(api): enqueue board-status sync on PR opened/reopened/ready"
```

---

### Task 4: Review-done trigger (worker enqueues)

**Files:**
- Modify: `worker/worker/runner.py` (`WorkerContext` ~line 82; `run_review` after `_post_result_to_github`, ~line 319; `build_worker_context` signature)
- Modify: `worker/worker/main.py` (~lines 25-45: build the Queue BEFORE the context and pass it in)
- Test: `worker/tests/test_runner.py`

**Interfaces:**
- Consumes: the Task 2 job path/params verbatim.
- Produces: `WorkerContext.rq_queue: Any | None = None` (default None keeps every existing test/fixture valid); a completed review enqueues `trigger="review_done"`.

- [ ] **Step 1: Write the failing tests**

In `worker/tests/test_runner.py`, find the existing `run_review` happy-path test (it builds a context via the file's fixture and asserts posting) and add, mirroring its setup:

```python
def test_completed_review_enqueues_board_status(...existing fixture args...):
    queue = MagicMock()
    # build the ctx exactly as the neighboring run_review test does, plus rq_queue=queue
    ...run the same happy-path run_review flow...
    call = queue.enqueue.call_args
    assert call.args[0] == "worker.board_status_tasks.run_board_status_update"
    assert call.args[1]["trigger"] == "review_done"
    assert call.args[1]["pr_number"] == <the fixture's pr number>


def test_declined_review_does_not_enqueue_board_status(...):
    # reuse the file's existing declined/budget path test setup with rq_queue=queue
    queue.enqueue.assert_not_called()


def test_board_status_enqueue_failure_never_fails_the_review(...):
    queue.enqueue.side_effect = RuntimeError("redis down")
    ...happy-path flow...
    # run_review still returns status completed
```

(Read the file's existing `run_review` tests first; reuse their fixtures verbatim — the three assertions are the requirement. If the file has no directly reusable happy-path fixture, use the closest one that reaches `_post_result_to_github` with a completed result.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_runner.py -k board_status -v`
Expected: FAIL — `WorkerContext` has no `rq_queue` / no enqueue happens

- [ ] **Step 3: Implement**

`worker/worker/runner.py`:

- `WorkerContext` gains (with the other defaulted fields):

```python
    # RQ queue handle so worker-side code can enqueue follow-up jobs
    # (board-status sync). None in tests/fixtures that don't need it.
    rq_queue: Any | None = None
```

(`Any` — add to the `typing` import if absent.)

- `build_worker_context(settings: Settings, rq_queue: Any | None = None)` — accept and set it on the constructed context.
- In `run_review`, directly after `_post_result_to_github(ctx, params, result, ...)` (line ~319) and before the final log/return:

```python
    if result.status == "completed" and ctx.rq_queue is not None:
        # Board-status sync (review_done leg). Fail-soft: a queue hiccup must
        # never fail a finished review — log + ops event and move on.
        try:
            from rq import Retry

            ctx.rq_queue.enqueue(
                "worker.board_status_tasks.run_board_status_update",
                {
                    "repo_full_name": f"{owner}/{name}".lower(),
                    "pr_number": pr_number,
                    "installation_id": params.installation_id,
                    "trigger": "review_done",
                },
                retry=Retry(max=3, interval=[30, 120, 300]),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("board_status_enqueue_failed", exc_info=True)
            writers.record_ops_event(
                ctx.db, "board_status", "warning", "enqueue_failed",
                {"repo": f"{owner}/{name}", "pr": pr_number, "error": str(exc)[:300]},
            )
```

`worker/worker/main.py` — reorder so the queue exists before the context, and pass it:

```python
    connection = Redis.from_url(settings.redis_url)
    queue = Queue(settings.queue_name, connection=connection)

    build_worker_context(settings, rq_queue=queue)

    worker = Worker([queue], connection=connection, name=socket.gethostname())
```

- [ ] **Step 4: Run the runner suite**

Run: `cd worker && .venv/bin/python -m pytest tests/test_runner.py -q`
Expected: all PASS (existing tests unaffected — `rq_queue` defaults to None)

- [ ] **Step 5: Commit**

```bash
git add worker/worker/runner.py worker/worker/main.py worker/tests/test_runner.py
git commit -m "feat(worker): completed reviews enqueue board-status review_done sync"
```

---

### Task 5: Verification sweep + docs sync

**Files:**
- Modify: `docs/superpowers/specs/2026-07-10-board-status-sync-design.md` (Status line + shared-helper deviation note)

- [ ] **Step 1: Full gates** (shared `reva/` touched → all three services)

Run: `make test` → worker/api/scheduler green.
Run: `ruff check reva worker/worker api/app scheduler/scheduler` → clean.
Run: `mypy reva worker/worker api/app scheduler/scheduler --ignore-missing-imports` → no NEW errors vs. main.
Run: `cd tui && go build ./... && go vet ./... && go test ./...` → green (TUI untouched).

- [ ] **Step 2: Spec sync**

Replace the Status line with:
`**Status:** Approved (Joseph, 2026-07-10) — implemented; see plans/2026-07-10-board-status-sync.md.`
And in the Design section where it says "extract the existing runner helper into a shared module rather than duplicating it": correct to reflect as-built — the job uses its own read-only Status resolver because the runner helper creates/renames fields, which this feature must never do (code wins over prose). Also correct the job-flow numbering: as built, link resolution and the DB lookup run BEFORE the config fetch (the kill switch is still checked in the job, but only once board items exist — saves a config fetch on every unrelated PR).

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-07-10-board-status-sync-design.md
git commit -m "docs(specs): board status sync — mark implemented, record resolver deviation"
```

**Honest-status note for the final report:** all coverage is unit-level (SQLite + MagicMock GitHub). Not exercised here: real Projects GraphQL against a live board, RQ retry behavior, and the end-to-end staging gate from the spec (open a linked PR → card moves to In Progress; review completes → In review; merge → Done via native workflow with no REVA interference).
