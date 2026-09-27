# Budget Wait-and-Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A job that hits a budget cap re-checks every 15 minutes and continues once spend rolls off, instead of failing for good; a consultant-facing `/reviews` page shows what is waiting and how full each budget is.

**Architecture:** Each gated RQ job defers *itself* with `queue.enqueue_in()` (the worker already runs `with_scheduler=True`) through one shared helper `defer_for_budget` in `worker/runner.py`, carrying `budget_wait_since` in its own params and giving up after a max wait. Rows keep their status (`pending` for Odoo work, a new `waiting_budget` status for reviews) plus a `budget_wait_since` column for visibility and requeue safety. The api serves `/reviews/` (static HTML) and `/reviews/data` (JSON) from the DB and RQ's scheduled registry; nginx proxies `/reviews/` like `/repo-docs/`.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2, RQ 2.9 (`enqueue_in`, `ScheduledJobRegistry`), pytest + SQLite in-memory, Go/Bubble Tea TUI, nginx.

**Spec:** `docs/superpowers/specs/2026-09-27-budget-wait-and-resume-design.md`

**Commits:** Joseph's rule is "never `git commit` unless I ask". Each task ends with a commit step; run it only if Joseph approved committing when he chose the execution method. Otherwise `git add` the files and stop at that step.

## Global Constraints

- Every new migration is idempotent (`ADD COLUMN IF NOT EXISTS`), numbered `050_...`, with the matching ORM column in `reva/db/models.py` (tests build from the models).
- Every caught-and-degraded error both logs AND `writers.record_ops_event(...)` (CLAUDE.md invariant "Degradations are visible").
- Every `REVA_*` env var the code reads must be documented in `.env.example` (`worker/tests/test_env_example.py` enforces it).
- New settings and their defaults, verbatim from the spec: `REVA_BUDGET_RETRY_SECONDS` = `900`, `REVA_BUDGET_WAIT_MAX_SECONDS` = `86400`. A retry value `<= 0` disables waiting (today's behaviour).
- Ops events: `budget_wait_started` (warning, first deferral only), `budget_wait_expired` (error, on give-up), `budget_wait_enqueue_failed` (error). Repeat checks only log.
- No Odoo-facing contract change: `contracts/` is NOT regenerated; row statuses Odoo polls stay `pending`/`completed`/`failed`.
- The `/reviews` page and `/reviews/data` carry no app-layer auth (same as `/repo-docs`); they never echo job args (ticket text).
- Definition of done: `make test` (worker, api, scheduler) green, `ruff check reva worker/worker api/app scheduler/scheduler` clean, `cd tui && go build ./... && go vet ./... && go test ./...` green.

## Review Focus

1. A deferred job whose params carry `budget_wait_since` as an ISO string with a `+00:00` offset (RQ pickles the dict; the api never sees it) must parse back to an aware datetime; a naive value must be treated as UTC, never crash. → Task 1 test `test_defer_parses_string_and_naive_since`.
2. A deferred ticket-analysis re-run while the row's `budget_wait_since` is set must clear the column once it passes the gate, so the page stops listing it and the requeue exemption ends. → Task 3 test `test_deferred_rerun_clears_budget_wait_marker`.
3. A deferred review re-run after the author pushed a new commit must not pay: the existing head-moved check returns `stale`. → Task 4 test `test_deferred_review_rerun_is_stale_when_head_moved`.
4. An Odoo requeue of a row that is waiting (column set, younger than max wait + stale window) must be refused with 409 so two paid runs never race. → Task 6 test `test_requeue_of_waiting_row_is_409`.
5. `/reviews/data` must still answer when Redis is unreachable (scheduled registry read fails): `waiting.jobs` empty, `jobs_error` set, ops event recorded. → Task 8 test `test_data_survives_registry_failure`.

---

### Task 1: `defer_for_budget` helper, settings, param field

**Files:**
- Modify: `worker/worker/settings.py` (Settings fields + `from_env`)
- Modify: `worker/worker/runner.py` (WorkerContext fields, `build_worker_context`, new helper)
- Modify: `reva/types.py` (six param models)
- Modify: `.env.example` (after line 54)
- Test: `worker/tests/test_budget_wait.py` (new)

**Interfaces:**
- Consumes: `writers.record_ops_event(db, component, severity, event, detail)`, `ctx.rq_queue` (an `rq.Queue`), `rq.get_current_job`.
- Produces:
  ```python
  def defer_for_budget(ctx: WorkerContext, task: str, job_params: dict, *,
                       kind: str, spent: float, log, job_timeout: int | None = None) -> dict | None
  ```
  Returns `{"status": "waiting_budget", "kind": kind, "spent_usd": float, "budget_wait_since": "<iso>", "retry_job_id": str, "retry_in_seconds": int}` after enqueueing, or `None` when the caller must take its terminal path (disabled, no queue, expired, enqueue failed).
  `WorkerContext.budget_retry_seconds: int = 900`, `WorkerContext.budget_wait_max_seconds: int = 86400`.
  Param models gain `budget_wait_since: datetime | None = None`.

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_budget_wait.py`:

```python
"""defer_for_budget: the shared wait-and-resume gate (spec 2026-09-27)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from reva.db import Base, Database, create_engine_from_url
from reva.db.models import OpsEvent
from worker.runner import WorkerContext, defer_for_budget


def _db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _ctx(queue=None, retry=900, max_wait=86400) -> WorkerContext:
    return WorkerContext(
        db=_db(), claude=None, runner=None, github=None,  # type: ignore[arg-type]
        reviewer=None, auditor=None, ticket_analyzer=None, verifier=None,  # type: ignore[arg-type]
        rq_queue=queue, budget_retry_seconds=retry, budget_wait_max_seconds=max_wait,
    )


def _events(ctx) -> list[tuple[str, str, str]]:
    with ctx.db.session() as s:
        return [(e.component, e.severity, e.event) for e in s.query(OpsEvent).all()]


def _queue():
    q = MagicMock()
    q.enqueue_in.return_value = MagicMock(id="rq:job:deferred-1")
    return q


def test_first_wait_stamps_since_and_enqueues_with_retry():
    q = _queue()
    ctx = _ctx(q)
    log = MagicMock()

    out = defer_for_budget(ctx, "worker.audit_tasks.run_audit", {"repository_id": 1},
                           kind="audit", spent=12.5, log=log, job_timeout=1234)

    assert out["status"] == "waiting_budget"
    assert out["retry_job_id"] == "rq:job:deferred-1"
    assert out["retry_in_seconds"] == 900
    delay, task, params = q.enqueue_in.call_args.args
    assert delay == timedelta(seconds=900)
    assert task == "worker.audit_tasks.run_audit"
    assert params["repository_id"] == 1
    since = datetime.fromisoformat(params["budget_wait_since"])
    assert since.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - since).total_seconds()) < 5
    kwargs = q.enqueue_in.call_args.kwargs
    assert kwargs["job_timeout"] == 1234
    assert kwargs["retry"].max == 3
    assert _events(ctx) == [("audit", "warning", "budget_wait_started")]


def test_repeat_wait_keeps_original_since_and_records_no_event():
    q = _queue()
    ctx = _ctx(q)
    since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    out = defer_for_budget(ctx, "t", {"budget_wait_since": since}, kind="audit",
                           spent=1.0, log=MagicMock())

    assert out["budget_wait_since"] == since
    assert q.enqueue_in.call_args.args[2]["budget_wait_since"] == since
    assert _events(ctx) == []


def test_defer_parses_string_and_naive_since():
    """RQ hands the dict back as pickled; a naive datetime or a string must both
    be treated as UTC rather than crash the gate (Review Focus 1)."""
    q = _queue()
    ctx = _ctx(q)
    naive = datetime.now() - timedelta(minutes=5)

    out = defer_for_budget(ctx, "t", {"budget_wait_since": naive}, kind="audit",
                           spent=1.0, log=MagicMock())

    assert out is not None
    assert datetime.fromisoformat(out["budget_wait_since"]).tzinfo is not None


def test_expired_wait_returns_none_with_error_event():
    q = _queue()
    ctx = _ctx(q, max_wait=3600)
    since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    out = defer_for_budget(ctx, "t", {"budget_wait_since": since}, kind="review",
                           spent=1.0, log=MagicMock())

    assert out is None
    q.enqueue_in.assert_not_called()
    assert _events(ctx) == [("review", "error", "budget_wait_expired")]


def test_disabled_or_no_queue_returns_none_silently():
    assert defer_for_budget(_ctx(_queue(), retry=0), "t", {}, kind="audit",
                            spent=1.0, log=MagicMock()) is None
    ctx = _ctx(None)
    assert defer_for_budget(ctx, "t", {}, kind="audit", spent=1.0, log=MagicMock()) is None
    assert _events(ctx) == []


def test_enqueue_failure_returns_none_with_error_event():
    q = _queue()
    q.enqueue_in.side_effect = ConnectionError("redis down")
    ctx = _ctx(q)

    out = defer_for_budget(ctx, "t", {}, kind="comment_reply", spent=1.0, log=MagicMock())

    assert out is None
    assert _events(ctx) == [("comment_reply", "error", "budget_wait_enqueue_failed")]


def test_job_timeout_taken_from_current_rq_job(monkeypatch):
    q = _queue()
    ctx = _ctx(q)
    job = MagicMock(timeout=2100, failure_ttl=86400)
    monkeypatch.setattr("worker.runner.get_current_job", lambda: job)

    defer_for_budget(ctx, "t", {}, kind="audit", spent=1.0, log=MagicMock(), job_timeout=5)

    kwargs = q.enqueue_in.call_args.kwargs
    assert kwargs["job_timeout"] == 2100
    assert kwargs["failure_ttl"] == 86400


def test_settings_defaults_and_env(monkeypatch, tmp_path):
    from worker.settings import Settings
    key = tmp_path / "key.pem"
    key.write_text("pem")
    for var in ("REDIS_URL", "DATABASE_URL", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(var, "x")
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_PATH", str(key))
    monkeypatch.delenv("REVA_BUDGET_RETRY_SECONDS", raising=False)
    monkeypatch.delenv("REVA_BUDGET_WAIT_MAX_SECONDS", raising=False)
    s = Settings.from_env()
    assert (s.budget_retry_seconds, s.budget_wait_max_seconds) == (900, 86400)

    monkeypatch.setenv("REVA_BUDGET_RETRY_SECONDS", "0")
    monkeypatch.setenv("REVA_BUDGET_WAIT_MAX_SECONDS", "3600")
    s = Settings.from_env()
    assert (s.budget_retry_seconds, s.budget_wait_max_seconds) == (0, 3600)


def test_params_models_accept_budget_wait_since():
    from reva.types import (AuditJobParams, JobParams, SupportJobParams,
                            TicketIssueJobParams, TicketJobParams, TimesheetJobParams)
    iso = "2026-09-27T10:00:00+00:00"
    assert JobParams(repository_id=1, pull_request_id=1, head_sha="a", installation_id=1,
                     trigger_event="opened", budget_wait_since=iso).budget_wait_since.tzinfo
    assert AuditJobParams(repository_id=1, installation_id=1).budget_wait_since is None
    for model in (TicketJobParams, SupportJobParams, TicketIssueJobParams, TimesheetJobParams):
        assert "budget_wait_since" in model.model_fields
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_budget_wait.py -v`
Expected: FAIL with `ImportError: cannot import name 'defer_for_budget'`.

- [ ] **Step 3: Settings**

In `worker/worker/settings.py`, after the `author_daily_budget_usd` field (line 42) add:

```python
    # Wait-and-resume for budget-gated jobs (spec 2026-09-27): a job that hits a
    # cap re-enqueues itself every `budget_retry_seconds` (<= 0 disables and
    # restores the old fail-fast behaviour) and gives up after
    # `budget_wait_max_seconds` from its first wait.
    budget_retry_seconds: int = 900
    budget_wait_max_seconds: int = 86400
```

In `from_env` after `author_daily_budget_usd=_author_daily_budget_from_env(),` add:

```python
            budget_retry_seconds=int(os.environ.get("REVA_BUDGET_RETRY_SECONDS", "900")),
            budget_wait_max_seconds=int(os.environ.get("REVA_BUDGET_WAIT_MAX_SECONDS", "86400")),
```

- [ ] **Step 4: WorkerContext + helper**

In `worker/worker/runner.py`, in `WorkerContext` after `author_daily_budget_usd: float | None = 100.0` add:

```python
    budget_retry_seconds: int = 900
    budget_wait_max_seconds: int = 86400
```

In `build_worker_context` after `author_daily_budget_usd=settings.author_daily_budget_usd,` add:

```python
        budget_retry_seconds=settings.budget_retry_seconds,
        budget_wait_max_seconds=settings.budget_wait_max_seconds,
```

After `instance_budget_exceeded` (ends line ~419) add:

```python
def _parse_wait_since(raw: object) -> datetime | None:
    """`budget_wait_since` as it comes back out of RQ: an ISO string (set by
    this helper), a datetime (a caller passed `params.model_dump()` without
    mode="json"), or absent. Naive values are UTC."""
    if raw is None:
        return None
    since = datetime.fromisoformat(raw) if isinstance(raw, str) else raw
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return since


def defer_for_budget(
    ctx: WorkerContext, task: str, job_params: dict, *, kind: str, spent: float,
    log, job_timeout: int | None = None,
) -> dict | None:
    """Wait-and-resume for a budget-gated job (spec 2026-09-27).

    Re-enqueues `task` with the same params `budget_retry_seconds` from now,
    stamping `budget_wait_since` on the first wait so the deadline survives
    the round trips. Returns the terminal result dict for THIS attempt, or
    None when the caller must take its old terminal path: waiting disabled,
    no queue handle, deadline passed, or the enqueue itself failed.
    """
    retry = ctx.budget_retry_seconds
    if retry <= 0 or ctx.rq_queue is None:
        return None
    now = datetime.now(timezone.utc)
    first_wait = job_params.get("budget_wait_since") is None
    since = _parse_wait_since(job_params.get("budget_wait_since")) or now
    waited = (now - since).total_seconds()
    if waited >= ctx.budget_wait_max_seconds:
        log.warning("budget_wait_expired", kind=kind, waited_s=int(waited),
                    spent_usd=round(spent, 2))
        writers.record_ops_event(ctx.db, kind, "error", "budget_wait_expired", {
            "task": task, "waited_seconds": int(waited), "spent_usd": round(spent, 2),
        })
        return None

    from rq import Retry

    job = get_current_job()
    timeout = getattr(job, "timeout", None) or job_timeout
    failure_ttl = getattr(job, "failure_ttl", None)
    params = dict(job_params)
    params["budget_wait_since"] = since.isoformat()
    try:
        deferred = ctx.rq_queue.enqueue_in(
            timedelta(seconds=retry), task, params,
            job_timeout=timeout, retry=Retry(max=3, interval=[30, 120, 300]),
            failure_ttl=failure_ttl,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("budget_wait_enqueue_failed", kind=kind, exc_info=True)
        writers.record_ops_event(ctx.db, kind, "error", "budget_wait_enqueue_failed", {
            "task": task, "error": str(exc)[:300],
        })
        return None
    if first_wait:
        writers.record_ops_event(ctx.db, kind, "warning", "budget_wait_started", {
            "task": task, "spent_usd": round(spent, 2), "retry_in_seconds": retry,
        })
    log.info("budget_wait_deferred", kind=kind, retry_in_seconds=retry,
             waited_s=int(waited), spent_usd=round(spent, 2), retry_job_id=deferred.id)
    return {
        "status": "waiting_budget",
        "kind": kind,
        "spent_usd": round(spent, 2),
        "budget_wait_since": since.isoformat(),
        "retry_job_id": deferred.id,
        "retry_in_seconds": retry,
    }
```

- [ ] **Step 5: Param models**

In `reva/types.py` add `budget_wait_since: datetime | None = None` as the LAST field of `JobParams` (after `trigger_event`), `TicketJobParams` (after `github_url`), `SupportJobParams` (after `images`), `TicketIssueJobParams` (after `release`), `TimesheetJobParams` (after `lines`), `AuditJobParams` (after `requested_by`), each with this comment on the line above:

```python
    # Set by worker.runner.defer_for_budget on a job that is waiting for budget
    # (spec 2026-09-27); None on every job the api enqueues.
```

Confirm `datetime` is already imported in `reva/types.py` (it is used by `AuditResult.started_at`).

- [ ] **Step 6: .env.example**

After line 54 (`# REVA_AUTHOR_DAILY_BUDGET_USD=100`) add:

```
# Budget-gated jobs wait for spend to roll off instead of failing: re-check
# interval (seconds; 0 disables waiting) and the maximum total wait.
# REVA_BUDGET_RETRY_SECONDS=900
# REVA_BUDGET_WAIT_MAX_SECONDS=86400
```

- [ ] **Step 7: Run the tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_budget_wait.py tests/test_env_example.py tests/test_config.py -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add worker/worker/settings.py worker/worker/runner.py reva/types.py .env.example worker/tests/test_budget_wait.py
git commit -m "feat(budget): 0000 - defer_for_budget helper, wait settings, budget_wait_since on job params"
```

---

### Task 2: Migration 050, ORM columns, writers for the wait marker

**Files:**
- Create: `db/migrations/050_budget_wait_since.sql`
- Modify: `reva/db/models.py` (`ReviewRun`, `TicketAnalysis`, `TicketIssueRun`, `TimesheetReviewRun`, `SupportTurn`)
- Modify: `reva/db/writers.py`
- Test: `worker/tests/test_budget_wait_writers.py` (new)

**Interfaces:**
- Produces:
  ```python
  BUDGET_WAIT_KINDS = ("ticket_analysis", "support_answer", "ticket_issues", "timesheet_review")
  def set_budget_wait_since(db, kind: str, row_id: int, since: datetime | None) -> None
  def list_budget_waiting(db) -> list[dict]   # {"kind","row_id","odoo_instance_id","instance_name","model_name","ticket_id","record","budget_wait_since"}
  def record_review_waiting_budget(db, params: JobParams, since: datetime, reason: str) -> int
  def list_reviews_waiting_budget(db) -> list[dict]  # {"id","repo_full_name","pr_number","pr_title","author_login","review_mode","budget_wait_since"}
  REVIEW_SPEND_KINDS = ("review", "delta_verify", "triage")
  ```
  Row dicts from `get_ticket_analysis`, `get_ticket_issue_run`, `get_timesheet_run`, `get_support_turn` gain key `budget_wait_since`.
  `is_already_posted` returns False for status `waiting_budget`.

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_budget_wait_writers.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.types import JobParams, TicketJobParams, TimesheetJobParams

SINCE = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _instance(db) -> int:
    return writers.create_odoo_instance(
        db, name="cu-prod", key_hash="h1", key_prefix="reva_odoo_aa",
        callback_url="", callback_api_key_enc="x",
    )


def test_set_and_clear_marker_on_ticket_analysis(db):
    inst = _instance(db)
    aid = writers.record_ticket_analysis_created(db, TicketJobParams(
        analysis_id=0, odoo_instance_id=inst, ticket_id=42, model_name="helpdesk.ticket",
        field_name="description", text="t"))
    assert writers.get_ticket_analysis(db, aid)["budget_wait_since"] is None

    writers.set_budget_wait_since(db, "ticket_analysis", aid, SINCE)
    row = writers.get_ticket_analysis(db, aid)
    assert row["status"] == "pending"
    assert row["budget_wait_since"].replace(tzinfo=timezone.utc) == SINCE

    writers.set_budget_wait_since(db, "ticket_analysis", aid, None)
    assert writers.get_ticket_analysis(db, aid)["budget_wait_since"] is None


def test_unknown_kind_is_a_programming_error(db):
    with pytest.raises(KeyError):
        writers.set_budget_wait_since(db, "nope", 1, SINCE)


def test_list_budget_waiting_spans_the_four_tables(db):
    inst = _instance(db)
    aid = writers.record_ticket_analysis_created(db, TicketJobParams(
        analysis_id=0, odoo_instance_id=inst, ticket_id=42, model_name="helpdesk.ticket",
        field_name="description", text="t"))
    ts = writers.record_timesheet_run_created(db, TimesheetJobParams(
        run_id=0, odoo_instance_id=inst, request_id="req-7", lines=[]))
    thread = writers.get_or_create_support_thread(
        db, odoo_instance_id=inst, ticket_id=99, model_name="project.task", field_name="f")
    turn = writers.record_support_turn_created(db, thread, inst, "Wie?")
    writers.set_budget_wait_since(db, "ticket_analysis", aid, SINCE)
    writers.set_budget_wait_since(db, "timesheet_review", ts, SINCE)
    writers.set_budget_wait_since(db, "support_answer", turn, SINCE)

    rows = writers.list_budget_waiting(db)

    by_kind = {r["kind"]: r for r in rows}
    assert set(by_kind) == {"ticket_analysis", "timesheet_review", "support_answer"}
    assert by_kind["ticket_analysis"]["record"] == "helpdesk.ticket 42"
    assert by_kind["ticket_analysis"]["instance_name"] == "cu-prod"
    assert by_kind["support_answer"]["record"] == "project.task 99"
    assert by_kind["timesheet_review"]["record"] == "timesheet req-7"
    assert all(r["budget_wait_since"] is not None for r in rows)


def _review_params(db) -> JobParams:
    repo_id = writers.upsert_repository(db, github_repository_id=1, owner="acme", name="w",
                                        default_branch="main", installation_id=5)
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=9, pr_number=42, title="Add foo",
        author_login="alice", base_branch="main", head_branch="f", head_sha="deadbeef",
        state="open", draft=False)
    return JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
                     installation_id=5, review_mode="diff", trigger_event="opened")


def test_review_waiting_budget_row_and_listing(db):
    params = _review_params(db)
    run_id = writers.record_review_waiting_budget(db, params, SINCE, "cap reached")

    rows = writers.list_reviews_waiting_budget(db)
    assert [r["id"] for r in rows] == [run_id]
    assert rows[0]["repo_full_name"] == "acme/w"
    assert rows[0]["pr_number"] == 42
    assert rows[0]["author_login"] == "alice"
    assert rows[0]["review_mode"] == "diff"
    # A queued Check Run on a waiting row is not a posted review.
    writers.attach_github_ids(db, run_id, check_run_id=777)
    assert writers.is_already_posted(db, params) is False
    # Re-claim works (non-running rows are re-claimable) and clears the marker.
    _, claimed = writers.claim_review_run(db, params, job_id="rq:2")
    assert claimed is True
    assert writers.list_reviews_waiting_budget(db) == []


def test_review_spend_kinds_exported():
    assert writers.REVIEW_SPEND_KINDS == ("review", "delta_verify", "triage")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_budget_wait_writers.py -v`
Expected: FAIL with `AttributeError: module 'reva.db.writers' has no attribute 'set_budget_wait_since'`.

- [ ] **Step 3: Migration**

Create `db/migrations/050_budget_wait_since.sql`:

```sql
-- Wait-and-resume for budget-gated jobs (spec 2026-09-27-budget-wait-and-resume).
-- Set on the first time a job defers itself for budget, cleared when the paid
-- work starts. Odoo-facing rows keep status 'pending' (dedup indexes + the
-- Odoo status contract are untouched); review_runs use status 'waiting_budget'.
-- Mirrors reva/db/models.py::{ReviewRun,TicketAnalysis,TicketIssueRun,
-- TimesheetReviewRun,SupportTurn}.budget_wait_since.
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS budget_wait_since TIMESTAMPTZ;
ALTER TABLE ticket_analyses ADD COLUMN IF NOT EXISTS budget_wait_since TIMESTAMPTZ;
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS budget_wait_since TIMESTAMPTZ;
ALTER TABLE timesheet_review_runs ADD COLUMN IF NOT EXISTS budget_wait_since TIMESTAMPTZ;
ALTER TABLE support_turns ADD COLUMN IF NOT EXISTS budget_wait_since TIMESTAMPTZ;
```

- [ ] **Step 4: ORM columns**

In `reva/db/models.py` add to each of `ReviewRun`, `TicketAnalysis`, `TicketIssueRun`, `TimesheetReviewRun`, `SupportTurn`, right after the `completed_at` column:

```python
    # Waiting for budget since (migration 050); NULL when not waiting.
    budget_wait_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 5: Writers**

In `reva/db/writers.py`:

(a) Near the top of the review section (before `claim_review_run`) add:

```python
# Ledger kinds that are PR-review spend: excluded from the global non-review
# cap, capped per author instead. Shared by the worker gate and the api's
# /reviews budget page so both read one definition.
REVIEW_SPEND_KINDS = ("review", "delta_verify", "triage")
```

(b) In `claim_review_run`, in the re-claim branch after `existing.trigger_event = params.trigger_event` add `existing.budget_wait_since = None`.

(c) After `record_review_declined` add:

```python
def record_review_waiting_budget(
    db: Database, params: JobParams, since: datetime, reason: str
) -> int:
    """Park a claimed run while its author's review cap is full (spec
    2026-09-27). Not terminal: the deferred job re-claims it later."""
    with db.session() as s:
        run = _upsert_review_run(s, params, status="waiting_budget")
        run.summary = reason
        run.decline_reason = None
        run.budget_wait_since = since
        s.flush()
        return run.id


def list_reviews_waiting_budget(db: Database) -> list[dict]:
    with db.session() as s:
        rows = s.execute(
            select(ReviewRun, Repository.full_name, PullRequest.pr_number,
                   PullRequest.title, PullRequest.author_login)
            .join(Repository, ReviewRun.repository_id == Repository.id)
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .where(ReviewRun.status == "waiting_budget")
            .order_by(Repository.full_name, ReviewRun.budget_wait_since)
        ).all()
        return [
            {
                "id": rr.id, "repo_full_name": full_name, "pr_number": pr_number,
                "pr_title": title, "author_login": author, "review_mode": rr.review_mode,
                "budget_wait_since": rr.budget_wait_since,
            }
            for rr, full_name, pr_number, title, author in rows
        ]
```

Confirm `Repository` and `PullRequest` are already in the `from reva.db.models import (...)` block; add them if not.

(d) In `is_already_posted` change the return to:

```python
    return bool(row and row[0] is not None and row[1] not in ("failed", "waiting_budget"))
```

and extend its docstring: "A `waiting_budget` run is excluded too — its check_run_id is the queued 'waiting for budget' check, not a review."

(e) After `instance`-related helpers near `sum_instance_cost_since` add:

```python
# ------------------------------------------------------------ budget waiting

BUDGET_WAIT_KINDS = ("ticket_analysis", "support_answer", "ticket_issues", "timesheet_review")
_BUDGET_WAIT_MODELS = {
    "ticket_analysis": TicketAnalysis,
    "support_answer": SupportTurn,
    "ticket_issues": TicketIssueRun,
    "timesheet_review": TimesheetReviewRun,
}


def set_budget_wait_since(db: Database, kind: str, row_id: int, since: datetime | None) -> None:
    """Stamp (or clear, since=None) the wait marker on one Odoo-facing run row.
    `kind` is one of BUDGET_WAIT_KINDS; anything else is a programming error."""
    model = _BUDGET_WAIT_MODELS[kind]
    with db.session() as s:
        row = s.get(model, row_id)
        if row is not None:
            row.budget_wait_since = since


def list_budget_waiting(db: Database) -> list[dict]:
    """Every Odoo-facing row currently waiting for budget, oldest first, with
    the instance name and a human record label for the /reviews page."""
    out: list[dict] = []
    with db.session() as s:
        names = dict(s.execute(select(OdooInstance.id, OdooInstance.name)).all())
        for kind, model in _BUDGET_WAIT_MODELS.items():
            rows = s.execute(
                select(model).where(model.budget_wait_since.is_not(None))
            ).scalars().all()
            for r in rows:
                if kind == "support_answer":
                    thread = s.get(SupportThread, r.thread_id)
                    model_name, ticket_id = thread.model_name, thread.ticket_id
                    record = f"{model_name} {ticket_id}"
                elif kind == "timesheet_review":
                    model_name, ticket_id = None, None
                    record = f"timesheet {r.request_id}"
                else:
                    model_name, ticket_id = r.model_name, r.ticket_id
                    record = f"{model_name} {ticket_id}"
                out.append({
                    "kind": kind, "row_id": r.id,
                    "odoo_instance_id": r.odoo_instance_id,
                    "instance_name": names.get(r.odoo_instance_id),
                    "model_name": model_name, "ticket_id": ticket_id, "record": record,
                    "budget_wait_since": r.budget_wait_since,
                })
    out.sort(key=lambda r: r["budget_wait_since"])
    return out
```

Confirm `OdooInstance`, `SupportThread`, `SupportTurn`, `TicketIssueRun`, `TimesheetReviewRun`, `TicketAnalysis` are imported from `reva.db.models` in writers (most already are; add the missing ones).

(f) Add `"budget_wait_since": row.budget_wait_since,` to the dicts returned by `get_ticket_analysis`, `get_ticket_issue_run`, `get_timesheet_run` (after `"completed_at"`), and add `"budget_wait_since"` to `_SUPPORT_TURN_FIELDS`.

- [ ] **Step 6: Worker uses the shared kinds**

In `worker/worker/runner.py` replace the module constant `_REVIEW_SPEND_KINDS = ("review", "delta_verify", "triage")` with `_REVIEW_SPEND_KINDS = writers.REVIEW_SPEND_KINDS` (keep the local name so `budget_exceeded` is untouched).

- [ ] **Step 7: Run the tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_budget_wait_writers.py tests/test_author_budget_writers.py tests/test_db.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add db/migrations/050_budget_wait_since.sql reva/db/models.py reva/db/writers.py worker/worker/runner.py worker/tests/test_budget_wait_writers.py
git commit -m "feat(budget): 0000 - budget_wait_since column + writers, waiting_budget review status"
```

---

### Task 3: Odoo-instance gates wait instead of failing

**Files:**
- Modify: `worker/worker/ticket_runner.py:212-220`
- Modify: `worker/worker/support_runner.py:104-108, 173-183`
- Modify: `worker/worker/ticket_issue_runner.py:996-1006`
- Modify: `worker/worker/timesheet_runner.py:88-100`
- Test: `worker/tests/test_ticket_runner.py`, `worker/tests/test_support_runner.py`, `worker/tests/test_ticket_issue_runner.py`, `worker/tests/test_timesheet_runner.py`

**Interfaces:**
- Consumes: `defer_for_budget(ctx, task, job_params, kind=..., spent=..., log=...)` (Task 1), `writers.set_budget_wait_since(db, kind, row_id, since)` (Task 2).
- Produces: each runner returns `{"status": "waiting_budget", ...}` while waiting; on give-up the existing failed path runs unchanged.

Pattern (identical in all four; the ticket runner shown in full):

```python
        spent = instance_budget_exceeded(ctx, params.odoo_instance_id)
        if spent is not None:
            waiting = defer_for_budget(
                ctx, "worker.ticket_tasks.run_ticket_analysis", params.model_dump(mode="json"),
                kind="ticket_analysis", spent=spent, log=log,
            )
            if waiting is not None:
                writers.set_budget_wait_since(
                    ctx.db, "ticket_analysis", params.analysis_id,
                    datetime.fromisoformat(waiting["budget_wait_since"]),
                )
                return waiting
            error = (...unchanged...)
            ...unchanged failed path...
        if params.budget_wait_since is not None:
            # Back from a wait and under the cap: clear the marker before paying.
            writers.set_budget_wait_since(ctx.db, "ticket_analysis", params.analysis_id, None)
```

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_ticket_runner.py`:

```python
def _waiting_queue():
    from unittest.mock import MagicMock
    q = MagicMock()
    q.enqueue_in.return_value = MagicMock(id="rq:job:deferred")
    return q


def test_instance_budget_gate_waits_and_keeps_row_pending(ctx_and_fakes, monkeypatch):
    """Over budget with waiting enabled: no paid call, no failure, row pending
    with the wait marker, the same job re-enqueued for later."""
    from dataclasses import replace
    s = ctx_and_fakes
    q = _waiting_queue()
    set_context(replace(s["ctx"], rq_queue=q))
    monkeypatch.setattr("worker.ticket_runner.instance_budget_exceeded", lambda ctx, iid: 12.5)
    params = _make_params(s["db"])

    out = run_ticket_analysis(params)

    assert out["status"] == "waiting_budget"
    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["status"] == "pending"
    assert row["budget_wait_since"] is not None
    assert s["analyzer"].call_count == 0
    _, task, deferred = q.enqueue_in.call_args.args
    assert task == "worker.ticket_tasks.run_ticket_analysis"
    assert deferred["analysis_id"] == params["analysis_id"]
    assert deferred["budget_wait_since"]


def test_deferred_rerun_clears_budget_wait_marker(ctx_and_fakes, monkeypatch):
    """Review Focus 2: back under the cap, the re-run clears the marker and completes."""
    from datetime import datetime, timedelta, timezone
    s = ctx_and_fakes
    params = _make_params(s["db"])
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    writers.set_budget_wait_since(s["db"], "ticket_analysis", params["analysis_id"], since)
    params["budget_wait_since"] = since.isoformat()

    out = run_ticket_analysis(params)

    assert out["status"] == "completed"
    assert writers.get_ticket_analysis(s["db"], params["analysis_id"])["budget_wait_since"] is None


def test_instance_budget_gate_fails_after_max_wait(ctx_and_fakes, monkeypatch):
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone
    s = ctx_and_fakes
    set_context(replace(s["ctx"], rq_queue=_waiting_queue(), budget_wait_max_seconds=3600))
    monkeypatch.setattr("worker.ticket_runner.instance_budget_exceeded", lambda ctx, iid: 12.5)
    params = _make_params(s["db"])
    params["budget_wait_since"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    with pytest.raises(PermanentError):
        run_ticket_analysis(params)

    assert writers.get_ticket_analysis(s["db"], params["analysis_id"])["status"] == "failed"
```

Append to `worker/tests/test_support_runner.py` (uses the module's `env` fixture and `_params(env)`):

```python
def test_over_instance_budget_waits_when_enabled(env):
    from dataclasses import replace
    from unittest.mock import MagicMock
    q = MagicMock()
    q.enqueue_in.return_value = MagicMock(id="rq:job:deferred")
    set_context(replace(env.ctx, rq_queue=q))
    env.monkeypatch.setattr("worker.support_runner.instance_budget_exceeded", lambda c, i: 12.5)

    out = run_support_answer(_params(env))

    assert out["status"] == "waiting_budget"
    row = writers.get_support_turn(env.db, env.turn_id)
    assert row["status"] == "pending"
    assert row["budget_wait_since"] is not None
    assert q.enqueue_in.call_args.args[1] == "worker.support_tasks.run_support_answer"
```

Append to `worker/tests/test_ticket_issue_runner.py` (fixture `ctx_and_fakes`, helper `_make_params`, next to `test_instance_budget_gate_declines_planning`):

```python
def test_instance_budget_gate_waits_when_queue_present(ctx_and_fakes, monkeypatch):
    from dataclasses import replace
    from unittest.mock import MagicMock
    s = ctx_and_fakes
    q = MagicMock()
    q.enqueue_in.return_value = MagicMock(id="rq:job:deferred")
    set_context(replace(s["ctx"], rq_queue=q))
    monkeypatch.setattr("worker.ticket_issue_runner.instance_budget_exceeded", lambda ctx, iid: 12.5)
    params = _make_params(s["db"])

    out = run_ticket_issues(params)

    assert out["status"] == "waiting_budget"
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["status"] == "pending" and row["budget_wait_since"] is not None
    assert s["planner"].call_count == 0
    assert s["odoo"].calls == []   # no failed callback while waiting
    assert q.enqueue_in.call_args.args[1] == "worker.ticket_issue_tasks.run_ticket_issues"
```

Append to `worker/tests/test_timesheet_runner.py` (fixture `ctx_and_fakes`, helper `_params`, next to `test_instance_budget_gate_declines_before_paid_call`):

```python
def test_instance_budget_gate_waits_when_queue_present(ctx_and_fakes, monkeypatch):
    from dataclasses import replace
    from unittest.mock import MagicMock
    s = ctx_and_fakes
    q = MagicMock()
    q.enqueue_in.return_value = MagicMock(id="rq:job:deferred")
    set_context(replace(s["ctx"], rq_queue=q))
    monkeypatch.setattr("worker.timesheet_runner.instance_budget_exceeded", lambda ctx, iid: 10.0)
    params = _params(s["db"])

    out = run_timesheet_review(params)

    assert out["status"] == "waiting_budget"
    row = writers.get_timesheet_run(s["db"], params["run_id"])
    assert row["status"] == "pending" and row["budget_wait_since"] is not None
    assert s["analyzer"].calls == []
    assert s["odoo"].calls == []
    assert q.enqueue_in.call_args.args[1] == "worker.timesheet_tasks.run_timesheet_review"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_runner.py tests/test_support_runner.py tests/test_ticket_issue_runner.py tests/test_timesheet_runner.py -k "wait or max_wait" -v`
Expected: the new tests FAIL (`PermanentError` raised / `KeyError: 'status'`).

- [ ] **Step 3: Ticket analysis**

In `worker/worker/ticket_runner.py`: add `from datetime import datetime` at the top and `defer_for_budget` to the `from worker.runner import ...` line. Replace lines 212-220 with the pattern above (kind `ticket_analysis`, task `worker.ticket_tasks.run_ticket_analysis`), keeping the existing `error = ...; log.warning(...); writers.record_ticket_analysis_failed(...); raise PermanentError(error)` as the give-up branch, and add the marker-clearing `if params.budget_wait_since is not None:` block right after it (before `try: version = repo_core_version(...)`).

- [ ] **Step 4: Support answer**

In `worker/worker/support_runner.py`: remove the gate at the top of `_produce_answer` (lines 175-183). In `run_support_answer`, inside the `else:` branch, change

```python
        try:
            html = _produce_answer(ctx, params, odoo, log)
```

to

```python
        try:
            spent = instance_budget_exceeded(ctx, params.odoo_instance_id)
            if spent is not None:
                waiting = defer_for_budget(
                    ctx, "worker.support_tasks.run_support_answer",
                    params.model_dump(mode="json"), kind="support_answer", spent=spent, log=log,
                )
                if waiting is not None:
                    writers.set_budget_wait_since(
                        ctx.db, "support_answer", params.turn_id,
                        datetime.fromisoformat(waiting["budget_wait_since"]),
                    )
                    return waiting
                log.warning("support_answer_instance_over_budget", spent_usd=round(spent, 2))
                raise PermanentError(
                    f"Odoo instance daily budget reached (~${spent:.2f} in 24h); "
                    f"support answer declined."
                )
            if params.budget_wait_since is not None:
                writers.set_budget_wait_since(ctx.db, "support_answer", params.turn_id, None)
            html = _produce_answer(ctx, params, odoo, log)
```

The existing `except Exception` below records the failed turn and the `answer_failed` ops event, so the give-up path stays terminal. Add the `datetime` import and `defer_for_budget` to the runner import. The task path is what `api/app/routes/v1/support_requests.py:73` enqueues.

- [ ] **Step 5: Ticket issues**

In `worker/worker/ticket_issue_runner.py` lines 997-1006, same pattern with kind `ticket_issues`, task `worker.ticket_issue_tasks.run_ticket_issues`, marker on `params.run_id`; keep `record_ticket_issue_run_failed` + `_send_failed_callback` + `raise PermanentError` as the give-up branch, and clear the marker right before `response, plan = ctx.ticket_issue_planner.plan_with_response(params)`.

- [ ] **Step 6: Timesheet**

In `worker/worker/timesheet_runner.py` lines 92-100, same pattern with kind `timesheet_review`, task `worker.timesheet_tasks.run_timesheet_review`, marker on `params.run_id`; clear the marker right after the gate (still inside `if remaining:`).

- [ ] **Step 7: Run the tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_runner.py tests/test_support_runner.py tests/test_ticket_issue_runner.py tests/test_timesheet_runner.py -v`
Expected: PASS, including the pre-existing over-budget tests (their contexts have `rq_queue=None`, so they still take the fail-fast path).

- [ ] **Step 8: Commit**

```bash
git add worker/worker/ticket_runner.py worker/worker/support_runner.py worker/worker/ticket_issue_runner.py worker/worker/timesheet_runner.py worker/tests/
git commit -m "feat(budget): 0000 - Odoo-instance gates wait for budget instead of failing"
```

---

### Task 4: PR-review gate waits with a queued Check Run

**Files:**
- Modify: `worker/worker/runner.py` (`run_review` lines 294-327, `_budget_decline_if_exceeded` lines 422-440, new `_post_waiting_check_run`)
- Test: `worker/tests/test_runner.py`

**Interfaces:**
- Consumes: `defer_for_budget`, `writers.record_review_waiting_budget`, `writers.reset_review_run_post_state`, `_check_run_id_or_recover`, `_create_or_update_check`, `REVIEW_JOB_TIMEOUT` (not yet imported in runner: add `from reva.claude_code_runner import REVIEW_JOB_TIMEOUT`).
- Produces: `run_review` returns the waiting dict; decline reason text changes.

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_runner.py` (module already has `ctx_and_fakes`, `_params`, `_seed_author_spend`, `_set_budget`, `_completed_result`, `_set_queue`):

```python
def _deferring_queue():
    q = MagicMock()
    q.enqueue_in.return_value = MagicMock(id="rq:job:deferred")
    return q


def test_review_waits_for_author_budget_with_queued_check(ctx_and_fakes):
    s = ctx_and_fakes
    _seed_author_spend(s, 5.0)
    _set_budget(s, 1.0)
    q = _deferring_queue()
    set_context(replace(get_context(), rq_queue=q))
    s["reviewer"].result = _completed_result()

    out = run_review(_params(s))

    assert out["status"] == "waiting_budget"
    assert s["reviewer"].call_count == 0
    assert s["github"].created_issue_comments == []          # no decline comment
    assert len(s["github"].created_check_runs) == 1
    check = s["github"].created_check_runs[0]
    assert check["status"] == "queued" and check["conclusion"] is None
    assert "budget" in check["output"]["title"].lower()
    from reva.db.models import ReviewRun
    with s["db"].session() as db_s:
        run = db_s.query(ReviewRun).filter_by(head_sha="deadbeef").one()
        assert run.status == "waiting_budget"
        assert run.budget_wait_since is not None
    _, task, deferred = q.enqueue_in.call_args.args
    assert task == "worker.tasks.run_review"
    assert deferred["head_sha"] == "deadbeef" and deferred["budget_wait_since"]


def test_deferred_review_rerun_completes_and_updates_queued_check(ctx_and_fakes):
    s = ctx_and_fakes
    _seed_author_spend(s, 5.0)
    _set_budget(s, 1.0)
    set_context(replace(get_context(), rq_queue=_deferring_queue()))
    s["reviewer"].result = _completed_result()
    first = run_review(_params(s))
    assert first["status"] == "waiting_budget"
    # The fake's find_check_run_id answers with recoverable_check_run_id; point
    # it at the queued check so the re-run finds it by SHA like GitHub would.
    s["github"].recoverable_check_run_id = s["github"].next_check_run_id - 1

    _set_budget(s, None)   # cap lifted / spend rolled off
    out = run_review(_params(s, budget_wait_since=first["budget_wait_since"]))

    assert out["status"] == "completed"
    assert s["reviewer"].call_count == 1
    # The queued check was found by SHA and updated in place, not duplicated.
    assert len(s["github"].created_check_runs) == 1
    assert len(s["github"].updated_check_runs) >= 1
    assert s["github"].updated_check_runs[-1]["status"] == "completed"


def test_deferred_review_rerun_is_stale_when_head_moved(ctx_and_fakes):
    """Review Focus 3: the author pushed meanwhile; the old SHA's re-run must not pay."""
    s = ctx_and_fakes
    s["reviewer"].result = ReviewResult(status="stale", summary="Head SHA changed", risk_level="low")
    out = run_review(_params(s, budget_wait_since="2026-09-27T08:00:00+00:00"))
    assert out["status"] == "stale"


def test_review_declines_after_max_wait_with_expired_text(ctx_and_fakes):
    s = ctx_and_fakes
    _seed_author_spend(s, 5.0)
    _set_budget(s, 1.0)
    set_context(replace(get_context(), rq_queue=_deferring_queue(), budget_wait_max_seconds=3600))
    s["reviewer"].result = _completed_result()
    since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    out = run_review(_params(s, budget_wait_since=since))

    assert out["status"] == "declined"
    assert "waited" in out["decline_reason"].lower()
    assert len(s["github"].created_issue_comments) == 1
```

`FakeGitHub.create_check_run` appends its kwargs to `created_check_runs` and returns `next_check_run_id` (then increments it); `update_check_run` appends to `updated_check_runs`; `find_check_run_id` returns `recoverable_check_run_id` (`worker/tests/test_runner.py:104-119`). Ensure `replace`, `get_context`, `ReviewResult`, `datetime`, `timedelta`, `timezone`, `MagicMock` are imported at the top of the test module (most are).

The existing `test_review_declined_when_author_over_budget` must keep passing: it has no `rq_queue`, so `defer_for_budget` returns None and the decline path runs. Its assertion `"$1" in out["decline_reason"]` must still hold after the text change below.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_runner.py -k "waits_for_author or deferred_review or expired_text" -v`
Expected: FAIL (`out["status"] == "declined"`).

- [ ] **Step 3: Implement**

In `worker/worker/runner.py`:

(a) Replace `_budget_decline_if_exceeded` with a pure formatter:

```python
def _decline_for_author_budget(
    ctx: WorkerContext, author: str | None, spent: float, *, waited: bool
) -> ReviewResult:
    """The declined ReviewResult for a full per-author cap. `waited` = the job
    already waited the maximum (spec 2026-09-27) and is giving up."""
    tail = (
        f"REVA waited {ctx.budget_wait_max_seconds / 3600:g} h for it to free up and gave up; "
        f"push again or comment `/review` later to retry."
        if waited else
        "Reviews resume automatically as spend rolls off."
    )
    reason = (
        f"REVA's rolling 24-hour review budget for @{author} "
        f"(${ctx.author_daily_budget_usd:.0f}) has been reached (≈${spent:.0f} spent). {tail}"
    )
    return ReviewResult(status="declined", summary="Author's daily review budget reached.",
                        risk_level="low", decline_reason=reason)
```

(b) In `run_review`, replace the block from `# Spend guard:` through `return budget_decline.model_dump(mode="json")` with:

```python
    # Spend guard: if the PR author's rolling 24-hour review spend has reached
    # their cap, wait for it to roll off (spec 2026-09-27) — decline only once
    # waiting is disabled or the maximum wait has passed.
    spent = author_budget_exceeded(ctx, params.pull_request_id)
    if spent is not None:
        author = pr_basic["author_login"]
        log.warning("review_over_author_budget", author=author,
                    spent_usd=round(spent, 2), budget_usd=ctx.author_daily_budget_usd)
        waiting = defer_for_budget(
            ctx, "worker.tasks.run_review", params.model_dump(mode="json"),
            kind="review", spent=spent, log=log, job_timeout=REVIEW_JOB_TIMEOUT,
        )
        if waiting is not None:
            since = datetime.fromisoformat(waiting["budget_wait_since"])
            writers.record_review_waiting_budget(
                ctx.db, params, since, f"Waiting for @{author}'s review budget to free up.")
            _post_waiting_check_run(ctx, params, run_id, owner, name, author, spent, log)
            log.info("review_job_done", status="waiting_budget")
            return waiting
        budget_decline = _decline_for_author_budget(
            ctx, author, spent, waited=params.budget_wait_since is not None)
        writers.record_review_declined(ctx.db, params, budget_decline.decline_reason or "Over budget.")
        _post_result_to_github(ctx, params, budget_decline, run_id, owner, name, pr_number, log)
        log.info("review_job_done", status="declined", reason="over_author_budget")
        return budget_decline.model_dump(mode="json")
```

(c) Right after the claim, change `if explicit:` to

```python
    if explicit or params.budget_wait_since is not None:
        # Re-review, or back from a budget wait: wipe the prior attempt's posted
        # IDs/outcome so the post step creates a fresh Check Run + PR Review
        # (the queued "waiting" check is found by SHA and updated in place).
        writers.reset_review_run_post_state(ctx.db, run_id)
```

(d) Add next to `_post_failure_check_run`:

```python
def _post_waiting_check_run(
    ctx: WorkerContext, params: JobParams, run_id: int, owner: str, name: str,
    author: str | None, spent: float, log,
) -> None:
    """Best-effort `queued` Check Run while the review waits for budget. No PR
    comment (no spam); the eventual result updates this check in place."""
    try:
        token = ctx.github.get_installation_token(params.installation_id)
        output = {
            "title": "Waiting for review budget",
            "summary": (
                f"@{author}'s rolling 24-hour review budget "
                f"(${ctx.author_daily_budget_usd:.0f}) is full (≈${spent:.0f} spent). "
                f"REVA re-checks every {ctx.budget_retry_seconds // 60} min and reviews "
                f"this commit as soon as spend rolls off."
            ),
        }
        check_run_id = _check_run_id_or_recover(
            ctx, token, owner, name, params.head_sha,
            lambda existing_id: _create_or_update_check(
                ctx, token, owner, name, existing_id,
                status="queued", conclusion=None, started_at=None, completed_at=None,
                output=output, head_sha=params.head_sha,
            ),
        )
        writers.attach_github_ids(ctx.db, run_id, check_run_id=check_run_id)
    except Exception:  # noqa: BLE001
        log.warning("waiting_check_run_post_failed", exc_info=True)
        writers.record_ops_event(ctx.db, "review", "warning", "waiting_check_post_failed", {
            "run_id": run_id, "repo": f"{owner}/{name}", "sha": params.head_sha[:8],
        })
```

(e) Grep for other callers of `_budget_decline_if_exceeded` (tests included) and update them to the new name/signature.

- [ ] **Step 4: Run the tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_runner.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add worker/worker/runner.py worker/tests/test_runner.py
git commit -m "feat(budget): 0000 - PR reviews wait for the author cap with a queued Check Run"
```

---

### Task 5: Global non-review gates (audit, comment reply, change note)

**Files:**
- Modify: `worker/worker/audit_tasks.py:131-137`
- Modify: `worker/worker/reply_runner.py:47-53`
- Modify: `worker/worker/change_note_runner.py:57-67`
- Test: `worker/tests/test_audit_tasks.py`, `worker/tests/test_comment_reply.py`, `worker/tests/test_change_note_delivery.py`

**Interfaces:**
- Consumes: `defer_for_budget`.
- Produces: `run_audit` / `run_comment_reply` / `run_change_note` return the waiting dict while waiting.

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_audit_tasks.py`:

```python
def test_run_audit_waits_when_over_budget_and_queue_present(db):
    from dataclasses import replace
    from unittest.mock import MagicMock
    d, repo_id = db
    writers.record_claude_spend(d, "audit", 50.0)
    auditor = FakeAuditor(_result(cost=3.5))
    q = MagicMock(); q.enqueue_in.return_value = MagicMock(id="rq:job:deferred")
    set_context(replace(_ctx(d, auditor, budget=10.0), rq_queue=q))

    out = run_audit({"repository_id": repo_id, "installation_id": 500})

    assert out["status"] == "waiting_budget"
    assert auditor.called is False
    assert q.enqueue_in.call_args.args[1] == "worker.audit_tasks.run_audit"
    assert q.enqueue_in.call_args.args[2]["repository_id"] == repo_id
```

Append to `worker/tests/test_comment_reply.py`:

```python
def test_reply_waits_when_over_budget_and_queue_present(db_with_finding):
    from dataclasses import replace
    writers.record_claude_spend(db_with_finding, "reply", 50.0)
    ctx = _ctx(db_with_finding, budget=10.0)
    q = MagicMock(); q.enqueue_in.return_value = MagicMock(id="rq:job:deferred")
    set_context(replace(ctx, rq_queue=q))

    out = run_comment_reply(_params())

    assert out["status"] == "waiting_budget"
    ctx.claude.chat.assert_not_called()
    assert q.enqueue_in.call_args.args[1] == "worker.tasks.run_comment_reply"
    assert q.enqueue_in.call_args.args[2]["comment_id"] == _COMMENT_ID
```

Append to `worker/tests/test_change_note_delivery.py` after `test_change_note_job_delivers_when_ticket_already_ready` (the `cn_ctx` fixture's ctx is a `SimpleNamespace`, so the wait knobs are set on it directly; the release-log lookup is unpatched there and yields no entry, so the job reaches the budget gate before `build_note`):

```python
def test_change_note_waits_when_over_budget_and_queue_present(cn_ctx, monkeypatch):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    q = MagicMock()
    q.enqueue_in.return_value = MagicMock(id="rq:job:deferred")
    s["ctx"].rq_queue = q
    s["ctx"].budget_retry_seconds = 900
    s["ctx"].budget_wait_max_seconds = 86400
    monkeypatch.setattr("worker.change_note_runner.budget_exceeded", lambda c: 50.0)
    _seed_run(s["db"], issues=[{"number": 50, "state": "closed"}])  # ready

    out = run_change_note(_cn_params())

    assert out["status"] == "waiting_budget"
    assert _note_rows(s["db"])[0].status == "pending"   # not skipped_budget
    assert s["odoo"].calls == []                         # nothing delivered yet
    assert q.enqueue_in.call_args.args[1] == "worker.change_note_tasks.run_change_note"
    assert q.enqueue_in.call_args.args[2]["pr_number"] == 7
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_audit_tasks.py tests/test_comment_reply.py tests/test_change_note_delivery.py -k waits -v`
Expected: FAIL.

- [ ] **Step 3: Audit**

In `worker/worker/audit_tasks.py` replace lines 133-137 with:

```python
    spent = budget_exceeded(ctx)
    if spent is not None:
        log.warning("audit_over_budget", spent_usd=round(spent, 2),
                    budget_usd=ctx.daily_budget_usd)
        waiting = defer_for_budget(
            ctx, "worker.audit_tasks.run_audit", job_params, kind="audit", spent=spent, log=log,
        )
        if waiting is not None:
            return waiting
        return {"audit_id": None, "status": "declined", "reason": "over_budget"}
```

Add `defer_for_budget` to the `from worker.runner import ...` line.

- [ ] **Step 4: Comment reply**

In `worker/worker/reply_runner.py` change the return annotation of `run_comment_reply` to `-> dict | None` and replace lines 49-53 with:

```python
    spent = budget_exceeded(ctx)
    if spent is not None:
        log.warning("reply_over_budget", spent_usd=round(spent, 2),
                    budget_usd=ctx.daily_budget_usd)
        return defer_for_budget(
            ctx, "worker.tasks.run_comment_reply", params, kind="comment_reply",
            spent=spent, log=log,
        )
```

(A `None` return is today's silent skip.) Add the import.

- [ ] **Step 5: Change note**

In `worker/worker/change_note_runner.py`, before the existing `spent = budget_exceeded(ctx)` line inside the loop, insert nothing; instead change the block to:

```python
            spent = budget_exceeded(ctx)
            if spent is not None:
                waiting = defer_for_budget(
                    ctx, "worker.change_note_tasks.run_change_note", job_params,
                    kind="change_note", spent=spent, log=logger,
                )
                if waiting is not None:
                    # Notes stay pending; delivery converges once the deferred
                    # run has drafted them.
                    return waiting
                writers.record_change_note_failed(
                    ctx.db, note_id, "skipped_budget", f"budget reached (~${spent:.0f})"
                )
                ...existing delivery test + continue unchanged...
```

Add the import.

- [ ] **Step 6: Run the tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_audit_tasks.py tests/test_comment_reply.py tests/test_change_note_delivery.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add worker/worker/audit_tasks.py worker/worker/reply_runner.py worker/worker/change_note_runner.py worker/tests/
git commit -m "feat(budget): 0000 - audits, replies and change notes wait for the global cap"
```

---

### Task 6: API: waiting rows are requeue-safe and visible

**Files:**
- Modify: `api/app/routes/v1/ticket_analyses.py` (`_is_stale_pending`, requeue), `support_requests.py`, `ticket_issues.py`, `timesheet_reviews.py`
- Modify: `api/app/schemas/ticket_analyses.py` (`TicketAnalysisStatus`, `TicketAnalysisSummary`), `support_requests.py` (`SupportTurnStatus`), `ticket_issues.py` (`TicketIssueRunSummary`, `TicketIssueRunStatus`), `timesheet_reviews.py` (`TimesheetReviewStatus`, `TimesheetReviewSummary`)
- Modify: `api/app/queries/ticket_analyses.py`, `api/app/queries/ticket_issues.py`, `api/app/queries/timesheet_reviews.py` (add the key to the item dicts)
- Create: `api/app/budget_wait.py`
- Test: `api/tests/test_v1_ticket_analyses.py`, `api/tests/test_v1_support_requests.py` (or the file that tests support requeue), `api/tests/test_v1_ticket_issues.py`

**Interfaces:**
- Produces `api/app/budget_wait.py`:
  ```python
  BUDGET_WAIT_MAX_SECONDS: int   # from REVA_BUDGET_WAIT_MAX_SECONDS, default 86400 (read via Settings, see Task 8)
  def is_waiting_for_budget(row: dict, stale_window: timedelta, max_wait_seconds: int) -> bool
  ```
  True when `row["budget_wait_since"]` is set and younger than `max_wait_seconds + stale_window`.
- All four routes: `_is_stale_pending(row)` returns False for a waiting row; requeue returns 409 with detail `"Waiting for budget; retries automatically"` for a waiting row.
- Schemas: `budget_wait_since: datetime | None = None` on the list and status models named above.

- [ ] **Step 1: Write the failing tests**

Append to `api/tests/test_v1_ticket_analyses.py`:

```python
def _mark_waiting(db, analysis_id, hours_ago: float, created_days_ago: int = 30):
    from datetime import datetime, timedelta, timezone
    from reva.db.models import TicketAnalysis
    with db.session() as s:
        row = s.get(TicketAnalysis, analysis_id)
        row.created_at = datetime.now(timezone.utc) - timedelta(days=created_days_ago)
        row.budget_wait_since = datetime.now(timezone.utc) - timedelta(hours=hours_ago)


def test_requeue_of_waiting_row_is_409(client_db_queue):
    """Review Focus 4: a row waiting for budget is not stale, even when old."""
    client, db, queue, headers = client_db_queue
    aid = client.post("/api/v1/ticket-analysis", json=BASE_PAYLOAD, headers=headers).json()["analysis_id"]
    _mark_waiting(db, aid, hours_ago=3)

    r = client.post(f"/api/v1/ticket-analysis/{aid}/requeue")

    assert r.status_code == 409
    assert "waiting for budget" in r.json()["detail"].lower()
    assert len(queue.enqueued) == 1


def test_waiting_row_becomes_stale_after_max_wait_plus_window(client_db_queue):
    client, db, queue, headers = client_db_queue
    aid = client.post("/api/v1/ticket-analysis", json=BASE_PAYLOAD, headers=headers).json()["analysis_id"]
    _mark_waiting(db, aid, hours_ago=24 + 4)   # past 24 h max wait + the 2.4 h stale window

    assert client.post(f"/api/v1/ticket-analysis/{aid}/requeue").status_code == 202


def test_status_and_list_expose_budget_wait_since(client_db_queue):
    client, db, queue, headers = client_db_queue
    aid = client.post("/api/v1/ticket-analysis", json=BASE_PAYLOAD, headers=headers).json()["analysis_id"]
    _mark_waiting(db, aid, hours_ago=1)

    assert client.get(f"/api/v1/ticket-analysis/{aid}").json()["budget_wait_since"] is not None
    items = client.get("/api/v1/ticket-analyses").json()["items"]
    assert items[0]["budget_wait_since"] is not None
```

The GET paths are `/api/v1/ticket-analysis/{id}` (shared gate) and `/api/v1/ticket-analyses` (master gate; the fixture sets no master key, so both are open).

Append to `api/tests/test_v1_support_requests.py` (fixture `client_db_queue`, helper `_post`):

```python
def test_requeue_of_waiting_turn_is_409(client_db_queue):
    from datetime import datetime, timedelta, timezone
    from reva.db.models import SupportTurn

    client, db, queue, headers = client_db_queue
    turn_id = _post(client, headers).json()["turn_id"]
    with db.session() as s:
        row = s.get(SupportTurn, turn_id)
        row.created_at = datetime.now(timezone.utc) - timedelta(days=30)
        row.budget_wait_since = datetime.now(timezone.utc) - timedelta(hours=3)

    r = client.post(f"/api/v1/support-turn/{turn_id}/requeue", headers=headers)

    assert r.status_code == 409
    assert "waiting for budget" in r.json()["detail"].lower()
    assert len(queue.enqueued) == 1
```

Append to `api/tests/test_v1_ticket_issues.py` (fixture `client_db_queue`, payload `CONTRACT_PAYLOAD`, next to `test_requeue_allowed_for_stale_pending`):

```python
def test_requeue_of_waiting_run_is_409(client_db_queue):
    from datetime import datetime, timedelta, timezone
    from reva.db.models import TicketIssueRun

    client, db, queue, headers = client_db_queue
    run_id = client.post("/api/v1/create-issues", json=CONTRACT_PAYLOAD, headers=headers).json()["request_id"]
    with db.session() as s:
        row = s.get(TicketIssueRun, run_id)
        row.created_at = datetime.now(timezone.utc) - timedelta(days=30)
        row.budget_wait_since = datetime.now(timezone.utc) - timedelta(hours=3)

    r = client.post(f"/api/v1/create-issues/{run_id}/requeue")

    assert r.status_code == 409
    assert "waiting for budget" in r.json()["detail"].lower()
    assert len(queue.enqueued) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd api && .venv/bin/python -m pytest tests -k "waiting_row or budget_wait_since" -v`
Expected: FAIL (`AttributeError: budget_wait_since` on the model is gone after Task 2; the requeue returns 202 / the JSON lacks the key).

- [ ] **Step 3: Shared predicate**

Create `api/app/budget_wait.py`:

```python
"""Requeue safety for rows that are waiting for budget (spec 2026-09-27).

A waiting row has a live scheduled job in Redis that will re-run it; letting
an ops/Odoo requeue start a second job would double-pay. The exemption ends
once the worker's maximum wait plus the route's own stale window has passed,
so a wait lost to a Redis flush still becomes requeueable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

WAITING_DETAIL = "Waiting for budget; retries automatically"


def is_waiting_for_budget(row: dict, stale_window: timedelta, max_wait_seconds: int) -> bool:
    since = row.get("budget_wait_since")
    if since is None:
        return False
    if since.tzinfo is None:  # SQLite returns naive datetimes
        since = since.replace(tzinfo=timezone.utc)
    return since > datetime.now(timezone.utc) - timedelta(seconds=max_wait_seconds) - stale_window
```

- [ ] **Step 4: Settings**

In `api/app/settings.py` add fields `budget_retry_seconds: int = 900` and `budget_wait_max_seconds: int = 86400`, plus `daily_budget_usd: float | None = None` and `author_daily_budget_usd: float | None = 100.0`, and in `from_env`:

```python
            budget_retry_seconds=int(os.environ.get("REVA_BUDGET_RETRY_SECONDS", "900")),
            budget_wait_max_seconds=int(os.environ.get("REVA_BUDGET_WAIT_MAX_SECONDS", "86400")),
            daily_budget_usd=(
                float(os.environ["REVA_DAILY_BUDGET_USD"])
                if os.environ.get("REVA_DAILY_BUDGET_USD") else None
            ),
            author_daily_budget_usd=_author_daily_budget_from_env(),
```

Copy `_author_daily_budget_from_env` verbatim from `worker/worker/settings.py:135-141` into `api/app/settings.py` (the two services do not import each other's settings modules). These are the same env names the worker reads, so `.env.example` already documents them.

In `docker-compose.yml` (api service environment block, after line 35) and `docker-compose.prod.yml` (after line 76) add:

```yaml
      REVA_DAILY_BUDGET_USD: ${REVA_DAILY_BUDGET_USD:-}
      REVA_AUTHOR_DAILY_BUDGET_USD: ${REVA_AUTHOR_DAILY_BUDGET_USD:-100}
      REVA_BUDGET_RETRY_SECONDS: ${REVA_BUDGET_RETRY_SECONDS:-900}
      REVA_BUDGET_WAIT_MAX_SECONDS: ${REVA_BUDGET_WAIT_MAX_SECONDS:-86400}
```

and the last two lines to the worker service blocks (after `REVA_AUTHOR_DAILY_BUDGET_USD` at lines 104 / 191).

- [ ] **Step 5: Routes**

In each of the four route modules:

`ticket_analyses.py` — change `_is_stale_pending` to

```python
def _is_stale_pending(row: dict, settings: Settings) -> bool:
    if is_waiting_for_budget(row, _STALE_PENDING, settings.budget_wait_max_seconds):
        return False
    created_at = row["created_at"]
    ...unchanged...
```

with `from app.budget_wait import WAITING_DETAIL, is_waiting_for_budget` and `Settings`/`get_settings` imported from `app.settings` / `app.dependencies`. In `requeue_ticket_analysis` add `settings: Settings = Depends(get_settings)` and change the guard to:

```python
    if is_waiting_for_budget(row, _STALE_PENDING, settings.budget_wait_max_seconds):
        raise HTTPException(status_code=409, detail=WAITING_DETAIL)
    if row["status"] not in ("failed", "completed") and not _is_stale_pending(row, settings):
```

`ticket_analyses.py` has no other `_is_stale_pending` call site.

`support_requests.py` (lines 125-129, 246) and `ticket_issues.py` (lines 366-370, 396): identical change. `timesheet_reviews.py` (lines 50-54; the only call site is `submit_timesheet_review` line 98): same predicate change, and `submit_timesheet_review` gets `settings: Settings = Depends(get_settings)` if it lacks one. There is no requeue route; a waiting run counts as live, so a re-submit of the same `request_id` takes the existing `existing is not None and not _is_stale_pending(...)` branch (returns the existing run) instead of starting a second one.

- [ ] **Step 6: Schemas and list queries**

Add `budget_wait_since: datetime | None = None` to `TicketAnalysisStatus`, `TicketAnalysisSummary`, `SupportTurnStatus`, `TicketIssueRunSummary`, `TicketIssueRunStatus`, `TimesheetReviewStatus`, `TimesheetReviewSummary`. Add `"budget_wait_since": r.budget_wait_since,` to the item dicts in `list_ticket_analyses`, `list_ticket_issue_runs`, `list_timesheet_reviews`. The status routes build from the writer dicts extended in Task 2, so they need no change.

- [ ] **Step 7: Run the tests**

Run: `cd api && .venv/bin/python -m pytest tests -v` and `cd worker && .venv/bin/python -m pytest tests/test_env_example.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add api/app docker-compose.yml docker-compose.prod.yml api/tests
git commit -m "feat(budget): 0000 - api treats waiting rows as live, exposes budget_wait_since"
```

---

### Task 7: Accept-time instance gate lets work wait (deviation, Joseph's call)

Today `assert_instance_within_budget` answers **429** at submit when the instance cap is full, so an over-budget Odoo request never becomes a row and there is nothing to resume. With waiting enabled the worker gate is the right place; the accept-time 429 makes the feature hollow for the most common case. **If Joseph rejects this task, skip it entirely; Tasks 3 and 6 still work for the case where the cap fills between accept and run.**

**Files:**
- Modify: `api/app/dependencies.py:97-115`
- Modify: `api/app/routes/v1/ticket_analyses.py:116`, `support_requests.py:144`, `ticket_issues.py:111`, `timesheet_reviews.py:89`
- Test: `api/tests/test_instance_quotas.py:110-118` (asserts the 429)

- [ ] **Step 1: Write the failing test**

In `api/tests/test_instance_quotas.py` the test at lines 110-118 asserts a 429 for an over-budget instance. Change its expectation: with `settings.budget_retry_seconds > 0` (the fixture default) the create returns **202** and one job is enqueued (`len(queue.enqueued) == 1`). Add a second test that builds the same situation but overrides `get_settings` with `Settings(..., budget_retry_seconds=0)` and asserts the 429 with "budget" in the detail and `queue.enqueued == []` (waiting disabled restores today's behaviour).

- [ ] **Step 2: Run to verify it fails**

Run: `cd api && .venv/bin/python -m pytest tests -k budget -v`
Expected: the changed test FAILS with 429.

- [ ] **Step 3: Implement**

Change `assert_instance_within_budget(db, instance)` to `assert_instance_within_budget(db, instance, settings: Settings)` and add as the first line:

```python
    if settings.budget_retry_seconds > 0:
        # Waiting is on (spec 2026-09-27): accept, let the worker gate park the
        # job until spend rolls off. Only a disabled wait fails fast here.
        return
```

Update the four call sites to pass their `settings` (each route already has or can add `settings: Settings = Depends(get_settings)`).

- [ ] **Step 4: Run the tests**

Run: `cd api && .venv/bin/python -m pytest tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/app/dependencies.py api/app/routes/v1 api/tests
git commit -m "feat(budget): 0000 - accept Odoo work when the instance cap is full and let the worker wait"
```

---

### Task 8: `/reviews` status page (api route, query module, static HTML)

**Files:**
- Create: `api/app/queries/budget_status.py`
- Create: `api/app/routes/budget_status.py`
- Create: `api/app/static/reviews.html`
- Modify: `api/app/main.py:75` (include router)
- Test: `api/tests/test_budget_status.py` (new)

**Interfaces:**
- Consumes: `writers.REVIEW_SPEND_KINDS`, `writers.sum_estimated_cost_since`, `writers.sum_instance_cost_since`, `writers.list_budget_waiting`, `writers.list_reviews_waiting_budget`, `app.queries.odoo_instances.list_odoo_instances`, `app.state.rq_queue`, `Settings.daily_budget_usd / author_daily_budget_usd / budget_retry_seconds / budget_wait_max_seconds`.
- Produces:
  ```python
  # api/app/queries/budget_status.py
  ROWLESS_KINDS = {"worker.audit_tasks.run_audit": "audit",
                   "worker.tasks.run_comment_reply": "comment_reply",
                   "worker.change_note_tasks.run_change_note": "change_note"}
  def review_cost_by_author_since(db, since) -> list[dict]        # {"author_login","spent_usd"} desc
  def scheduled_budget_jobs(queue, db) -> list[dict]               # {"kind","target","budget_wait_since","next_run_at"}
  def build_status(db, queue, settings) -> dict                    # the JSON below
  ```
  JSON shape:
  ```json
  {"generated_at": "...", "settings": {"retry_seconds": 900, "max_wait_seconds": 86400},
   "budgets": {"global": {"spent_usd": 0.0, "cap_usd": null, "over": false},
               "instances": [{"id":1,"name":"...","spent_usd":0.0,"cap_usd":null,"over":false}],
               "authors": [{"author_login":"...","spent_usd":0.0,"cap_usd":100.0,"over":false}]},
   "waiting": {"reviews": [{"repo_full_name":"...","count":2,"authors":["a"],"oldest_since":"...",
                            "prs":[{"id":1,"pr_number":1,"pr_title":"...","author_login":"a","review_mode":"diff","budget_wait_since":"..."}]}],
               "odoo": [{"kind":"ticket_analysis","instance_name":"...","record":"helpdesk.ticket 1","budget_wait_since":"..."}],
               "jobs": [{"kind":"audit","target":"acme/w","budget_wait_since":"...","next_run_at":"..."}],
               "jobs_error": null}}
  ```

- [ ] **Step 1: Write the failing tests**

Create `api/tests/test_budget_status.py`:

```python
"""Tests for the consultant-facing /reviews budget page and its JSON."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import OpsEvent, ReviewRun
from reva.types import JobParams, TicketJobParams

SINCE = datetime.now(timezone.utc) - timedelta(hours=2)


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine_from_url("sqlite:///:memory:",
                                    connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(database_url="sqlite:///:memory:", github_app_id=1,
                        github_webhook_secret="x", github_private_key="x",
                        redis_url="redis://localhost:6379/0",
                        daily_budget_usd=200.0, author_daily_budget_usd=100.0)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.rq_queue = object()
    monkeypatch.setattr("app.queries.budget_status.scheduled_budget_jobs", lambda queue, db: [])
    yield TestClient(app), db, monkeypatch
    app.dependency_overrides.clear()


def _seed_review_waiting(db, *, author="alice", pr_number=42, cost=0.0):
    repo_id = writers.upsert_repository(db, github_repository_id=1, owner="acme", name="w",
                                        default_branch="main", installation_id=5)
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=9000 + pr_number, pr_number=pr_number,
        title=f"PR {pr_number}", author_login=author, base_branch="main", head_branch="f",
        head_sha=f"sha{pr_number}", state="open", draft=False)
    params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha=f"sha{pr_number}",
                       installation_id=5, review_mode="diff", trigger_event="opened")
    run_id = writers.record_review_waiting_budget(db, params, SINCE, "cap")
    if cost:
        with db.session() as s:
            run = s.get(ReviewRun, run_id)
            run.estimated_cost_usd = cost
            run.completed_at = datetime.now(timezone.utc)
    return run_id


def test_page_is_html(env):
    client, _, _ = env
    r = client.get("/reviews/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Waiting" in r.text


def test_data_empty_state(env):
    client, _, _ = env
    body = client.get("/reviews/data").json()
    assert body["settings"] == {"retry_seconds": 900, "max_wait_seconds": 86400}
    assert body["budgets"]["global"] == {"spent_usd": 0.0, "cap_usd": 200.0, "over": False}
    assert body["budgets"]["instances"] == [] and body["budgets"]["authors"] == []
    assert body["waiting"] == {"reviews": [], "odoo": [], "jobs": [], "jobs_error": None}


def test_data_groups_waiting_reviews_per_repo_and_flags_over_cap_author(env):
    client, db, _ = env
    _seed_review_waiting(db, pr_number=42, cost=120.0)   # alice: over the $100 cap
    _seed_review_waiting(db, pr_number=43)
    writers.record_claude_spend(db, "audit", 250.0)       # global cap $200: over

    body = client.get("/reviews/data").json()

    assert body["budgets"]["global"]["over"] is True
    authors = body["budgets"]["authors"]
    assert authors[0]["author_login"] == "alice" and authors[0]["over"] is True
    groups = body["waiting"]["reviews"]
    assert len(groups) == 1
    assert groups[0]["repo_full_name"] == "acme/w"
    assert groups[0]["count"] == 2 and groups[0]["authors"] == ["alice"]
    assert [p["pr_number"] for p in groups[0]["prs"]] == [42, 43]


def test_data_lists_waiting_odoo_rows_with_instance_budget(env):
    client, db, _ = env
    inst = writers.create_odoo_instance(
        db, name="cu-prod", key_hash="h1", key_prefix="reva_odoo_aa",
        callback_url="", callback_api_key_enc="x",
    )
    writers.update_odoo_instance(db, inst, daily_budget_usd=60.0)
    aid = writers.record_ticket_analysis_created(db, TicketJobParams(
        analysis_id=0, odoo_instance_id=inst, ticket_id=6791, model_name="helpdesk.ticket",
        field_name="description", text="t"))
    writers.set_budget_wait_since(db, "ticket_analysis", aid, SINCE)

    body = client.get("/reviews/data").json()

    inst_row = body["budgets"]["instances"][0]
    assert inst_row["name"] == "cu-prod" and inst_row["cap_usd"] == 60.0
    odoo = body["waiting"]["odoo"]
    assert odoo == [{"kind": "ticket_analysis", "instance_name": "cu-prod",
                     "record": "helpdesk.ticket 6791", "budget_wait_since": odoo[0]["budget_wait_since"]}]


def test_data_includes_rowless_jobs_from_registry(env):
    client, _, monkeypatch = env
    monkeypatch.setattr("app.queries.budget_status.scheduled_budget_jobs", lambda queue, db: [
        {"kind": "audit", "target": "acme/w", "budget_wait_since": SINCE.isoformat(),
         "next_run_at": (SINCE + timedelta(minutes=15)).isoformat()},
    ])
    body = client.get("/reviews/data").json()
    assert body["waiting"]["jobs"][0]["kind"] == "audit"


def test_data_survives_registry_failure(env):
    """Review Focus 5: Redis down must not take the page down."""
    client, db, monkeypatch = env

    def boom(queue, db):
        raise ConnectionError("redis down")
    monkeypatch.setattr("app.queries.budget_status.scheduled_budget_jobs", boom)

    body = client.get("/reviews/data").json()

    assert body["waiting"]["jobs"] == []
    assert "redis down" in body["waiting"]["jobs_error"]
    with db.session() as s:
        events = [(e.component, e.event) for e in s.query(OpsEvent).all()]
    assert ("budget_status", "scheduled_registry_unavailable") in events


def test_scheduled_budget_jobs_reads_registry(monkeypatch):
    """The real reader: only jobs carrying budget_wait_since, args never echoed."""
    from types import SimpleNamespace
    from app.queries import budget_status as bs

    jobs = {
        "j1": SimpleNamespace(func_name="worker.audit_tasks.run_audit",
                              args=({"repository_id": 7, "budget_wait_since": SINCE.isoformat()},)),
        "j2": SimpleNamespace(func_name="worker.tasks.run_comment_reply",
                              args=({"owner": "acme", "repo": "w", "pr_number": 3, "question": "secret?"},)),
        "j3": SimpleNamespace(func_name="worker.change_note_tasks.run_change_note",
                              args=({"repo_full_name": "acme/w", "pr_number": 9, "pr_body": "secret",
                                     "budget_wait_since": SINCE.isoformat()},)),
    }
    queue = SimpleNamespace(fetch_job=lambda jid: jobs[jid])
    fake_registry = SimpleNamespace(
        get_job_ids=lambda: list(jobs),
        get_scheduled_time=lambda jid: SINCE + timedelta(minutes=15),
    )
    monkeypatch.setattr(bs, "ScheduledJobRegistry", lambda queue: fake_registry)
    monkeypatch.setattr(bs, "_repo_full_name", lambda db, repo_id: "acme/w")

    out = bs.scheduled_budget_jobs(queue, db=object())

    assert [j["kind"] for j in out] == ["audit", "change_note"]   # j2 has no budget_wait_since
    assert out[0]["target"] == "acme/w" and out[1]["target"] == "acme/w #9"
    assert "secret" not in str(out)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd api && .venv/bin/python -m pytest tests/test_budget_status.py -v`
Expected: FAIL with 404s / `ModuleNotFoundError: app.queries.budget_status`.

- [ ] **Step 3: Query module**

Create `api/app/queries/budget_status.py`:

```python
"""Data for the consultant-facing /reviews page (spec 2026-09-27): how full each
budget is and what is waiting for one. Read-only; nothing here echoes job args."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from rq.registry import ScheduledJobRegistry
from sqlalchemy import func, select

from app.queries.odoo_instances import list_odoo_instances
from app.settings import Settings
from reva.db import writers
from reva.db.engine import Database
from reva.db.models import PullRequest, Repository, ReviewRun

logger = structlog.get_logger()

# Deferred job types that have no DB row while waiting; read from RQ's
# scheduled registry instead. Row-backed kinds (reviews, Odoo work) come from
# the DB so a Redis flush cannot hide them.
ROWLESS_KINDS = {
    "worker.audit_tasks.run_audit": "audit",
    "worker.tasks.run_comment_reply": "comment_reply",
    "worker.change_note_tasks.run_change_note": "change_note",
}


def review_cost_by_author_since(db: Database, since: datetime) -> list[dict]:
    """Paid review spend per PR author in the window, highest first — the same
    rows sum_author_review_cost_since counts, grouped."""
    with db.session() as s:
        rows = s.execute(
            select(PullRequest.author_login, func.sum(ReviewRun.estimated_cost_usd))
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .where(ReviewRun.completed_at >= since, ReviewRun.estimated_cost_usd > 0,
                   PullRequest.author_login.is_not(None))
            .group_by(PullRequest.author_login)
            .order_by(func.sum(ReviewRun.estimated_cost_usd).desc())
        ).all()
    return [{"author_login": login, "spent_usd": round(float(total), 2)} for login, total in rows]


def _repo_full_name(db: Database, repository_id: int) -> str:
    with db.session() as s:
        name = s.execute(select(Repository.full_name).where(Repository.id == repository_id)).scalar()
    return name or f"repo {repository_id}"


def _target(db: Database, kind: str, params: dict) -> str:
    if kind == "audit":
        return _repo_full_name(db, params.get("repository_id"))
    if kind == "comment_reply":
        return f"{params.get('owner')}/{params.get('repo')} #{params.get('pr_number')}"
    return f"{params.get('repo_full_name')} #{params.get('pr_number')}"


def scheduled_budget_jobs(queue, db: Database) -> list[dict]:
    """Deferred row-less jobs waiting in RQ's scheduled registry. `db` resolves
    an audit's repository id to its name; the test patches `_repo_full_name`."""
    registry = ScheduledJobRegistry(queue=queue)
    out: list[dict] = []
    for job_id in registry.get_job_ids():
        job = queue.fetch_job(job_id)
        if job is None:
            continue
        kind = ROWLESS_KINDS.get(job.func_name)
        params = job.args[0] if job.args and isinstance(job.args[0], dict) else {}
        if kind is None or params.get("budget_wait_since") is None:
            continue
        next_run = registry.get_scheduled_time(job_id)
        out.append({
            "kind": kind,
            "target": _target(db, kind, params),
            "budget_wait_since": params["budget_wait_since"],
            "next_run_at": next_run.isoformat() if next_run else None,
        })
    out.sort(key=lambda j: j["budget_wait_since"])
    return out


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _group_reviews(rows: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(r["repo_full_name"], {
            "repo_full_name": r["repo_full_name"], "count": 0, "authors": [],
            "oldest_since": None, "prs": [],
        })
        g["count"] += 1
        if r["author_login"] and r["author_login"] not in g["authors"]:
            g["authors"].append(r["author_login"])
        since = _iso(r["budget_wait_since"])
        if since and (g["oldest_since"] is None or since < g["oldest_since"]):
            g["oldest_since"] = since
        g["prs"].append({
            "id": r["id"], "pr_number": r["pr_number"], "pr_title": r["pr_title"],
            "author_login": r["author_login"], "review_mode": r["review_mode"],
            "budget_wait_since": since,
        })
    return [groups[k] for k in sorted(groups)]


def build_status(db: Database, queue, settings: Settings) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=1)
    global_spent = writers.sum_estimated_cost_since(db, since, exclude_kinds=writers.REVIEW_SPEND_KINDS)
    global_cap = settings.daily_budget_usd
    instances = []
    for inst in list_odoo_instances(db):
        spent = writers.sum_instance_cost_since(db, inst["id"], since)
        cap = inst["daily_budget_usd"]
        instances.append({"id": inst["id"], "name": inst["name"], "spent_usd": round(spent, 2),
                          "cap_usd": cap, "over": cap is not None and spent >= cap})
    author_cap = settings.author_daily_budget_usd
    authors = [
        {**a, "cap_usd": author_cap, "over": author_cap is not None and a["spent_usd"] >= author_cap}
        for a in review_cost_by_author_since(db, since)
    ]

    jobs: list[dict] = []
    jobs_error: str | None = None
    try:
        jobs = scheduled_budget_jobs(queue, db)
    except Exception as exc:  # noqa: BLE001 — Redis down must not take the page down
        jobs_error = str(exc)[:300]
        logger.warning("budget_status_registry_unavailable", error=jobs_error)
        writers.record_ops_event(db, "budget_status", "warning", "scheduled_registry_unavailable",
                                 {"error": jobs_error})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settings": {"retry_seconds": settings.budget_retry_seconds,
                     "max_wait_seconds": settings.budget_wait_max_seconds},
        "budgets": {
            "global": {"spent_usd": round(global_spent, 2), "cap_usd": global_cap,
                       "over": global_cap is not None and global_spent >= global_cap},
            "instances": instances,
            "authors": authors,
        },
        "waiting": {
            "reviews": _group_reviews(writers.list_reviews_waiting_budget(db)),
            "odoo": [
                {"kind": r["kind"], "instance_name": r["instance_name"], "record": r["record"],
                 "budget_wait_since": _iso(r["budget_wait_since"])}
                for r in writers.list_budget_waiting(db)
            ],
            "jobs": jobs,
            "jobs_error": jobs_error,
        },
    }
```

`list_odoo_instances` (`api/app/queries/odoo_instances.py:85`) folds a per-instance cost rollup in; that is one extra query per instance on a page refreshed once a minute, acceptable. Only `id`, `name` and `daily_budget_usd` are read here.

- [ ] **Step 4: Route**

Create `api/app/routes/budget_status.py`:

```python
"""Consultant-facing budget page (`/reviews`, spec 2026-09-27).

Like /repo-docs this router carries no app-layer auth: a Cloudflare Access
application must gate the `/reviews` prefix at the edge (docs/setup-production.md).
Read-only. Job args are never echoed.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from app.dependencies import get_db, get_settings
from app.queries.budget_status import build_status
from app.settings import Settings
from reva.db.engine import Database

router = APIRouter()
_PAGE = Path(__file__).resolve().parent.parent / "static" / "reviews.html"


@router.get("/", include_in_schema=False)
def page() -> FileResponse:
    return FileResponse(_PAGE, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})


@router.get("/data")
def data(request: Request, db: Database = Depends(get_db),
         settings: Settings = Depends(get_settings)) -> dict:
    return build_status(db, getattr(request.app.state, "rq_queue", None), settings)
```

In `api/app/main.py` after the docs router line add:

```python
# Consultant budget page — gated by Cloudflare Access like /docs, not the API key.
app.include_router(budget_status.router, prefix="/reviews")
```

and add `budget_status` to the `from app.routes import ...` import. The api Dockerfile copies `api/app/` whole (`COPY api/app/ ./app/`, line 22), so `static/reviews.html` ships with no Dockerfile change.

- [ ] **Step 5: Static page**

Create `api/app/static/reviews.html` — the mockup's layout, driven by `fetch("data")`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>REVA budgets</title>
<style>
  :root { --bg:#f5f6f8; --surface:#fff; --line:#dde1e7; --ink:#1d2430; --muted:#64707f;
          --accent:#2f5fa8; --wait:#b7791f; --over:#b23a3a; --over-soft:#f9e3e3;
          --accent-soft:#e4ecf8; --track:#e8ebf0; }
  @media (prefers-color-scheme: dark) { :root { color-scheme:dark; --bg:#14181e; --surface:#1c2229;
          --line:#2c343e; --ink:#e6eaef; --muted:#98a3b1; --accent:#7ea6e6; --wait:#e0a94a;
          --over:#e57373; --over-soft:#3f2222; --accent-soft:#223247; --track:#2a323c; } }
  body { margin:0; padding:0 16px 48px; background:var(--bg); color:var(--ink);
         font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
  .wrap { max-width:1080px; margin:0 auto; display:grid; gap:28px; }
  header { display:flex; flex-wrap:wrap; justify-content:space-between; align-items:baseline;
           gap:8px 24px; padding:22px 0 14px; border-bottom:1px solid var(--line); }
  h1 { font-size:20px; margin:0; } h1 span { color:var(--muted); font-weight:400; }
  .meta { color:var(--muted); font-size:13px; }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:0 0 10px; }
  .budgets { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:16px; }
  .panel { background:var(--surface); border:1px solid var(--line); border-radius:6px; padding:14px 16px; }
  .panel h3 { font-size:14px; margin:0 0 12px; display:flex; justify-content:space-between; }
  .panel h3 small { color:var(--muted); font-weight:400; font-size:12px; }
  .meter { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:4px 12px; align-items:center;
           padding:7px 0; border-top:1px solid var(--line); }
  .meter:first-of-type { border-top:0; padding-top:0; }
  .num { font-family:ui-monospace,Menlo,monospace; font-size:12.5px; color:var(--muted); white-space:nowrap; }
  .num b { color:var(--ink); font-weight:500; }
  .track { grid-column:1/-1; height:6px; background:var(--track); border-radius:3px; overflow:hidden; }
  .fill { height:100%; background:var(--accent); } .fill.warn { background:var(--wait); } .fill.over { background:var(--over); }
  .pill { font-size:11px; font-weight:600; text-transform:uppercase; padding:1px 7px; border-radius:3px; }
  .pill.over { background:var(--over-soft); color:var(--over); } .pill.off { background:var(--accent-soft); color:var(--accent); }
  .repo { background:var(--surface); border:1px solid var(--line); border-radius:6px; margin-top:8px; }
  .repo summary { list-style:none; cursor:pointer; display:grid; grid-template-columns:auto minmax(0,1fr) auto;
                  gap:12px; align-items:center; padding:10px 14px; }
  .repo summary::-webkit-details-marker { display:none; }
  .chev { color:var(--muted); font-size:11px; } .repo[open] .chev { transform:rotate(90deg); display:inline-block; }
  .title { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .title .sub { font-weight:400; color:var(--muted); font-size:12.5px; margin-left:8px; }
  .right { font-family:ui-monospace,Menlo,monospace; font-size:12.5px; color:var(--muted); white-space:nowrap; }
  .tablewrap { overflow-x:auto; background:var(--surface); border:1px solid var(--line); border-radius:6px; }
  .repo .tablewrap { border:0; border-top:1px solid var(--line); border-radius:0 0 6px 6px; }
  table { border-collapse:collapse; width:100%; min-width:560px; }
  th, td { text-align:left; padding:9px 14px; border-top:1px solid var(--line); vertical-align:top; }
  th { border-top:0; font-size:11.5px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
  td.mono { font-family:ui-monospace,Menlo,monospace; font-size:12.5px; white-space:nowrap; }
  td .sub { display:block; color:var(--muted); font-size:12px; }
  tr.stripe { border-left:3px solid var(--wait); } tr.stripe.over { border-left-color:var(--over); }
  .empty { padding:14px; color:var(--muted); } .count { color:var(--muted); margin-left:6px; font-weight:400; }
  .note { color:var(--muted); font-size:12.5px; margin:8px 0 0; } .err { color:var(--over); }
  @media (max-width:560px) { .repo summary { grid-template-columns:auto minmax(0,1fr); } .right { grid-column:2; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>REVA <span>/ budgets and waiting work</span></h1>
    <div class="meta" id="meta">Loading…</div>
  </header>
  <section><h2>Budgets, rolling 24 h</h2><div class="budgets" id="budgets"></div></section>
  <section><h2>Waiting PR reviews <span class="count" id="rev-count"></span></h2><div id="reviews"></div></section>
  <section><h2>Waiting Odoo work <span class="count" id="odoo-count"></span></h2><div id="odoo"></div></section>
  <section><h2>Other deferred jobs <span class="count" id="jobs-count"></span></h2><div id="jobs"></div>
    <p class="note">Read-only. Requeue and cap changes stay in the TUI.</p></section>
</div>
<script>
(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const money = (v) => v == null ? "—" : "$" + Number(v).toFixed(2);
  const hm = (iso) => iso ? new Date(iso).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) : "—";
  const ago = (iso) => {
    if (!iso) return "";
    const m = Math.max(0, Math.round((Date.now() - new Date(iso)) / 60000));
    return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
  };
  const OPEN_KEY = "reva.reviews.open";
  const openRepos = () => { try { return new Set(JSON.parse(localStorage.getItem(OPEN_KEY) || "[]")); } catch { return new Set(); } };
  const saveOpen = (set) => { try { localStorage.setItem(OPEN_KEY, JSON.stringify([...set])); } catch {} };

  function meter(name, spent, cap, over) {
    const pct = cap ? Math.min(100, Math.round(spent / cap * 100)) : (spent > 0 ? 4 : 0);
    const cls = over ? "over" : (pct >= 75 ? "warn" : "");
    const capTxt = cap == null ? `<span class="pill off">no cap</span>` : `${money(cap)}${over ? ' <span class="pill over">over</span>' : ""}`;
    return `<div class="meter"><div>${esc(name)}</div><div class="num"><b>${money(spent)}</b> / ${capTxt}</div>
            <div class="track"><div class="fill ${cls}" style="width:${pct}%"></div></div></div>`;
  }
  function panel(title, small, rows, emptyText) {
    return `<div class="panel"><h3>${title} <small>${small}</small></h3>${rows.length ? rows.join("") : `<div class="note">${emptyText}</div>`}</div>`;
  }
  function render(d) {
    const s = d.settings;
    $("meta").textContent = `Refreshed ${hm(d.generated_at)} · every 60 s · waiting jobs re-check every ${Math.round(s.retry_seconds / 60)} min, give up after ${Math.round(s.max_wait_seconds / 3600)} h`;
    const g = d.budgets.global;
    $("budgets").innerHTML =
      panel("Global, non-review calls", "audits, replies, change notes", [meter("All instances and repos", g.spent_usd, g.cap_usd, g.over)], "") +
      panel("Odoo instances", "ticket analysis, support, issues, timesheets", d.budgets.instances.map((i) => meter(i.name, i.spent_usd, i.cap_usd, i.over)), "No Odoo instances.") +
      panel("PR authors", "review spend, last 24 h", d.budgets.authors.map((a) => meter(a.author_login, a.spent_usd, a.cap_usd, a.over)), "No paid reviews in the last 24 h.");

    const groups = d.waiting.reviews;
    const total = groups.reduce((n, g) => n + g.count, 0);
    $("rev-count").textContent = total ? `${total} in ${groups.length} repo${groups.length === 1 ? "" : "s"}` : "";
    const open = openRepos();
    $("reviews").innerHTML = groups.length ? groups.map((g) => `
      <details class="repo" data-repo="${esc(g.repo_full_name)}" ${open.has(g.repo_full_name) ? "open" : ""}>
        <summary><span class="chev">&#9654;</span>
          <span class="title">${esc(g.repo_full_name)} <span class="sub">${g.count} PR${g.count === 1 ? "" : "s"} · ${esc(g.authors.join(", "))}</span></span>
          <span class="right">oldest ${hm(g.oldest_since)} (${ago(g.oldest_since)})</span></summary>
        <div class="tablewrap"><table><thead><tr><th>PR</th><th>Author</th><th>Mode</th><th>Waiting since</th></tr></thead><tbody>
          ${g.prs.map((p) => `<tr class="stripe"><td>#${p.pr_number} <span class="sub">${esc(p.pr_title)}</span></td><td>${esc(p.author_login)}</td><td>${esc(p.review_mode)}</td><td class="mono">${hm(p.budget_wait_since)} (${ago(p.budget_wait_since)})</td></tr>`).join("")}
        </tbody></table></div></details>`).join("") : `<div class="tablewrap"><div class="empty">Nothing waiting.</div></div>`;
    document.querySelectorAll("details.repo").forEach((el) => el.addEventListener("toggle", () => {
      const set = openRepos(); el.open ? set.add(el.dataset.repo) : set.delete(el.dataset.repo); saveOpen(set);
    }));

    const KIND = {ticket_analysis: "Ticket analysis", support_answer: "Support answer", ticket_issues: "Issue planning",
                  timesheet_review: "Timesheet review", audit: "Repo audit", comment_reply: "Comment reply", change_note: "Change note"};
    const odoo = d.waiting.odoo;
    $("odoo-count").textContent = odoo.length || "";
    $("odoo").innerHTML = odoo.length ? `<div class="tablewrap"><table><thead><tr><th>Kind</th><th>Instance</th><th>Record</th><th>Waiting since</th><th>Gives up</th></tr></thead><tbody>
      ${odoo.map((r) => { const giveUp = new Date(new Date(r.budget_wait_since).getTime() + s.max_wait_seconds * 1000).toISOString();
        return `<tr class="stripe"><td>${KIND[r.kind] || esc(r.kind)}</td><td>${esc(r.instance_name)}</td><td class="mono">${esc(r.record)}</td><td class="mono">${hm(r.budget_wait_since)} (${ago(r.budget_wait_since)})</td><td class="mono">${new Date(giveUp).toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit"})}</td></tr>`; }).join("")}
      </tbody></table></div>` : `<div class="tablewrap"><div class="empty">Nothing waiting.</div></div>`;

    const jobs = d.waiting.jobs;
    $("jobs-count").textContent = jobs.length || "";
    $("jobs").innerHTML = (jobs.length ? `<div class="tablewrap"><table><thead><tr><th>Kind</th><th>Target</th><th>Waiting since</th><th>Next run</th></tr></thead><tbody>
      ${jobs.map((j) => `<tr class="stripe"><td>${KIND[j.kind] || esc(j.kind)}</td><td>${esc(j.target)}</td><td class="mono">${hm(j.budget_wait_since)} (${ago(j.budget_wait_since)})</td><td class="mono">${hm(j.next_run_at)}</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="tablewrap"><div class="empty">Nothing waiting.</div></div>`) +
      (d.waiting.jobs_error ? `<p class="note err">Job queue unreachable: ${esc(d.waiting.jobs_error)}</p>` : "");
  }
  async function load() {
    try {
      const res = await fetch("data", {cache: "no-store"});
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      render(await res.json());
    } catch (e) {
      $("meta").innerHTML = `<span class="err">Could not load: ${esc(e.message)}</span>`;
    }
  }
  load();
  setInterval(load, 60000);
})();
</script>
</body>
</html>
```

- [ ] **Step 6: Run the tests**

Run: `cd api && .venv/bin/python -m pytest tests/test_budget_status.py tests/test_docs.py -v`
Expected: PASS.

- [ ] **Step 7: Look once**

Run the api locally (`make dev`) and open `http://localhost:8080/reviews/` once; fix anything visibly broken (a clipped column, an empty panel that should show "Nothing waiting."). One pass, no loop.

- [ ] **Step 8: Commit**

```bash
git add api/app/queries/budget_status.py api/app/routes/budget_status.py api/app/static/reviews.html api/app/main.py api/tests/test_budget_status.py
git commit -m "feat(budget): 0000 - /reviews page shows budgets and work waiting for one"
```

---

### Task 9: nginx location and docs

**Files:**
- Modify: `nginx/templates/reva.conf.template` (after the `/repo-docs/` block, ~line 101)
- Modify: `docs/setup-production.md:171-176`
- Modify: `docs-ui/README.md` (diagram + Access paragraph)
- Modify: `docs/user.md:142-145`
- Modify: `worker/README.md` (find the section that describes spend caps: `grep -n -i "cap\|spend" worker/README.md`)
- Modify: `CLAUDE.md` "Cost control" invariant bullet

- [ ] **Step 1: nginx**

After the `/repo-docs/` location add:

```nginx
    # Consultant budget page (/reviews + /reviews/data), served by the api.
    # Gate with the same Cloudflare Access application as /docs.
    location = /reviews { return 301 /reviews/; }
    location /reviews/ {
        limit_req zone=api burst=20 nodelay;
        proxy_pass http://$api_upstream:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
```

and extend the section comment above (`The SPA (/docs) and its data API (/repo-docs) are the only human-facing surface`) to name `/reviews` too. Validate: `docker compose -f docker-compose.prod.yml build nginx` (or `nginx -t` inside the image) if Docker is available; otherwise state that the template was not syntax-checked.

- [ ] **Step 2: Docs**

`docs/setup-production.md`: change "covering the paths `/docs` and `/repo-docs`" to "covering the paths `/docs`, `/repo-docs` and `/reviews` (the consultant docs SPA, its data API, and the budget status page)". `docs-ui/README.md`: add a `├─ /reviews/ → api (budget page)` line to the diagram and "/reviews" to the Access paragraph. `docs/user.md` cost-control bullet: "... caps park work until spend rolls off (re-checked every 15 min, up to 24 h) instead of overspending; `/reviews` shows what is waiting." `worker/README.md`: one paragraph on the wait-and-resume behaviour and the two env vars. `CLAUDE.md` cost-control bullet: append "A job that hits a cap defers itself via RQ (`defer_for_budget`, `REVA_BUDGET_RETRY_SECONDS` / `REVA_BUDGET_WAIT_MAX_SECONDS`) instead of failing; `/reviews` (edge-gated like `/docs`) lists waiting work and budget fill."

- [ ] **Step 3: Commit**

```bash
git add nginx/templates/reva.conf.template docs/setup-production.md docs-ui/README.md docs/user.md worker/README.md CLAUDE.md
git commit -m "docs(budget): 0000 - /reviews nginx location, Access prefix, wait-and-resume notes"
```

---

### Task 10: TUI shows waiting state

**Files:**
- Modify: `tui/internal/api/types.go` (`TicketAnalysisSummary`, `TicketIssueRunSummary`, `TimesheetReviewSummary`, `SupportTurnDetail`)
- Modify: `tui/internal/ui/tickets.go` (`rowText` ~line 570, detail extras ~line 655, `ticketStatusSymbol`/`plainStatusSymbol`)
- Modify: `tui/internal/ui/reviews.go` (status filter cycle line 125-134)
- Modify: `tui/internal/ui/styles.go` (`statusSymbol`, `statusChar`)
- Modify: `tui/internal/api/mock.go` (one waiting analysis row, one `waiting_budget` review)
- Test: `tui/internal/ui/tickets_test.go`, `tui/internal/ui/reviews_test.go`, `tui/internal/ui/styles_test.go`

- [ ] **Step 1: Write the failing tests**

Append to `tui/internal/ui/tickets_test.go`:

```go
func TestAnalysisRowShowsWaitingForBudget(t *testing.T) {
	since := time.Now().Add(-90 * time.Minute)
	a := api.TicketAnalysisSummary{ID: 1, TicketID: 7, ModelName: "helpdesk.ticket", Status: "pending",
		CreatedAt: since, BudgetWaitSince: &since}
	got := analysisStatusText(a)
	if !strings.Contains(got, "waiting for budget") || !strings.Contains(got, "1h30m") {
		t.Fatalf("expected waiting text with duration, got %q", got)
	}
}
```

Append to `tui/internal/ui/styles_test.go`:

```go
func TestStatusSymbolWaitingBudget(t *testing.T) {
	if statusChar("waiting_budget") != "~" {
		t.Fatalf("waiting_budget should render as ~ (pending style)")
	}
}
```

Append to `tui/internal/ui/reviews_test.go` (add `tea "github.com/charmbracelet/bubbletea"` to its imports):

```go
func TestReviewsStatusFilterCycleIncludesWaitingBudget(t *testing.T) {
	r := newReviews(&api.MockClient{})
	r.width, r.height = 200, 30
	press := func() { r, _ = r.update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("s")}) }
	want := []string{"completed", "failed", "stale", "waiting_budget", ""}
	for i, w := range want {
		press()
		if r.statusFilter != w {
			t.Fatalf("press %d: statusFilter = %q, want %q", i+1, r.statusFilter, w)
		}
	}
}
```

If `r.update` is not the method that handles key messages in `reviews.go` (check `TestReviewsListShowsCarriedFromLabel`, which calls `r.update(reviewsLoadedMsg{...})`), use the same method it uses.

- [ ] **Step 2: Run to verify they fail**

Run: `cd tui && go test ./internal/ui/`
Expected: compile error (`BudgetWaitSince` undefined, `analysisStatusText` undefined).

- [ ] **Step 3: Types**

In `types.go` add to `TicketAnalysisSummary`, `TicketIssueRunSummary`, `TimesheetReviewSummary`, `SupportTurnDetail`:

```go
	// BudgetWaitSince is set while the row waits for its Odoo instance's budget
	// to roll off (spec 2026-09-27); nil otherwise. Status stays "pending".
	BudgetWaitSince *time.Time `json:"budget_wait_since"`
```

- [ ] **Step 4: Tickets tab**

In `tickets.go` add:

```go
// analysisStatusText is the status cell for an analysis row: a pending row that
// is parked on budget says so instead of looking stuck.
func analysisStatusText(a api.TicketAnalysisSummary) string {
	if a.Status == "pending" && a.BudgetWaitSince != nil {
		return "~ waiting for budget " + time.Since(*a.BudgetWaitSince).Truncate(time.Minute).String()
	}
	return strings.TrimSpace(plainStatusSymbol(a.Status, a.CreatedAt) + " " + a.Status)
}
```

and in `rowText` replace the `else` branch that builds `analysisPlain`/`analysisColored` with:

```go
			} else if a.Status == "pending" && a.BudgetWaitSince != nil {
				analysisPlain = analysisStatusText(a)
				analysisColored = styleStatusStale.Render(analysisPlain)
			} else {
```

(keep the existing else body after it). Do the same for the issue-run cell: when `run.Status == "pending" && run.BudgetWaitSince != nil`, render `"~ waiting for budget"` in `styleStatusStale`. In the detail extras (~line 655) add, when `a.BudgetWaitSince != nil`, a `styleSubtitle` line `"  waiting for budget since HH:MM (re-checked every 15 min)"`.

- [ ] **Step 5: Reviews tab + styles**

In `reviews.go` extend the `s` cycle: `case "stale": r.statusFilter = "waiting_budget"` then `default: r.statusFilter = ""`. In `styles.go` add `case "waiting_budget": return styleStatusStale.Render("~")` to `statusSymbol` and `case "waiting_budget": return "~"` to `statusChar`. The requeue guard in `reviews.go:198` already rejects unknown statuses; leave it.

- [ ] **Step 6: Mock**

In `mock.go` add one `TicketAnalysisSummary` with `Status: "pending", BudgetWaitSince: &tWait` (`tWait := now.Add(-40 * time.Minute)`) and one `ReviewSummary` with `Status: "waiting_budget"` so `go run . --demo` shows both.

- [ ] **Step 7: Build, vet, test**

Run: `cd tui && go build ./... && go vet ./... && go test ./...`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add tui/
git commit -m "feat(tui): 0000 - show waiting-for-budget on tickets and reviews"
```

---

### Task 11: Whole-change verification and handoff notes

**Files:**
- Modify: `HANDOFF.md` (add a short "budget wait-and-resume" entry at the top with: what shipped, the migration number, the Access prefix ops step, and "not live-validated: RQ `enqueue_in` round trip and the Postgres migration")
- Move: `docs/superpowers/specs/2026-09-27-budget-wait-and-resume-design.md` → `docs/superpowers/specs/archive/`, and this plan → `docs/superpowers/plans/archive/` (CLAUDE.md: specs/plans hold open work only)

- [ ] **Step 1: Full suites**

Run:

```bash
make test
ruff check reva worker/worker api/app scheduler/scheduler
cd tui && go build ./... && go vet ./... && go test ./...
```

Expected: all green. If `make test` skips a service because its `.venv` is missing, create it per CLAUDE.md and rerun.

- [ ] **Step 2: Staging validation checklist (not runnable in unit tests; record in HANDOFF.md)**

1. Boot once against Postgres: migration 050 applies (`\d ticket_analyses` shows `budget_wait_since`).
2. Set `REVA_BUDGET_RETRY_SECONDS=60` and an instance cap of `0.01`; submit a ticket analysis; expect a `waiting_budget` job result, the row `pending` with the marker, `/reviews/` listing it, and after raising the cap the job re-firing within ~60 s and completing.
3. Open `/reviews/` through the tunnel: Cloudflare Access must challenge (prefix added).

- [ ] **Step 3: HANDOFF + archive moves**

Write the HANDOFF.md entry; `git mv` the spec and plan into their `archive/` folders.

- [ ] **Step 4: Commit**

```bash
git add HANDOFF.md docs/superpowers
git commit -m "docs(budget): 0000 - handoff notes, archive spec and plan"
```
