# Ops-Event Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Persistent, TUI-visible log of every caught-and-degraded component error (CodeGraph fallbacks, callback failures, git retries) — plan 1 of 2 from the odoo-core-knowledge spec.

**Architecture:** One append-only `ops_events` table + a safe-to-fail writer; hook points retrofitted onto today's silent degradations (CodeGraph first, via an injected recorder callback so `ClaudeCodeRunner` stays DB-free); `GET /api/v1/ops-events`; the TUI Failures tab gains a toggleable "component events" view and the dashboard a degradations counter; 30-day retention in the existing purge pass.

**Tech Stack:** Python 3.14 (SQLAlchemy/FastAPI), Go Bubble Tea. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-05-odoo-core-knowledge-design.md` §6 (+ error-handling table). Plan 2 (core knowledge) consumes the writer this plan creates.

## Global Constraints

- Per-service venvs (`worker/.venv` etc.); shared `reva/` change → final gate `make test` + `ruff check reva worker/worker api/app scheduler/scheduler`; TUI gate `cd tui && go build ./... && go vet ./... && go test ./...`.
- Migrations idempotent, `BIGSERIAL`, `TIMESTAMPTZ`; ORM mirrors with `_PK` SQLite variant. **Check the next free migration number first** — as of writing, pending plans claim 025/026; this plan uses **027** (`ls db/migrations/ | sort | tail`).
- **`record_ops_event` must never break the operation it observes** — it swallows and logs its own failures.
- Component slugs (fixed vocabulary, plan 2 adds more): `codegraph`, `git`, `odoo_callback` (later: `core_knowledge`, `ticket_planner`, `retrieval`). Severity: `warning` | `error`.
- New invariant lands in CLAUDE.md (Task 7): *any caught-and-degraded error must both log and record an ops event.*

---

### Task 1: DB — `ops_events` table, ORM, writers

**Files:**
- Create: `db/migrations/027_ops_events.sql`
- Modify: `reva/db/models.py` (new model after `ClaudeSpend`, ~line 648), `reva/db/writers.py` (new section + import)
- Test: `worker/tests/test_ops_events.py`

**Interfaces:**
- Produces (all later tasks depend on these exact names):
  - `reva.db.models.OpsEvent` (`id, component, severity, event, detail, created_at`)
  - `writers.record_ops_event(db: Database, component: str, severity: str, event: str, detail: dict | None = None) -> None` — safe-to-fail
  - `writers.purge_old_ops_events(db: Database, older_than_days: int) -> int`

- [x] **Step 1: Write the failing tests**

Create `worker/tests/test_ops_events.py`:

```python
"""ops_events: safe-to-fail writer + retention purge (spec §6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import OpsEvent


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def test_record_and_read(db):
    writers.record_ops_event(
        db, "codegraph", "warning", "index_failed",
        {"repo": "acme/widgets", "error": "exit 1"},
    )
    with db.session() as s:
        row = s.query(OpsEvent).one()
    assert row.component == "codegraph"
    assert row.severity == "warning"
    assert row.event == "index_failed"
    assert row.detail["repo"] == "acme/widgets"
    assert row.created_at is not None


def test_detail_optional(db):
    writers.record_ops_event(db, "git", "warning", "fetch_timeout")
    with db.session() as s:
        assert s.query(OpsEvent).one().detail is None


def test_writer_swallows_db_failure(db, monkeypatch):
    """The observer must never break the observed operation."""
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "session", boom)
    # Must not raise:
    writers.record_ops_event(db, "codegraph", "error", "index_failed", {})


def test_purge_old_events(db):
    writers.record_ops_event(db, "git", "warning", "old")
    with db.session() as s:
        s.query(OpsEvent).one().created_at = (
            datetime.now(timezone.utc) - timedelta(days=40)
        )
    writers.record_ops_event(db, "git", "warning", "fresh")

    assert writers.purge_old_ops_events(db, older_than_days=30) == 1
    with db.session() as s:
        assert s.query(OpsEvent).one().event == "fresh"
    assert writers.purge_old_ops_events(db, older_than_days=30) == 0
```

- [x] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ops_events.py -q`
Expected: FAIL — `ImportError: cannot import name 'OpsEvent'`

- [x] **Step 3: Create the migration**

Confirm the number: `ls db/migrations/ | sort | tail -3` (take the next free; 027 assumed below). `db/migrations/027_ops_events.sql`:

```sql
-- Persistent component-degradation log (ops-event spec §6): every
-- caught-and-degraded error (CodeGraph fallback, callback failure, git retry,
-- core-knowledge degradation) is recorded here so a quietly-degrading system
-- is visible in the TUI, not only in container logs. Append-only; purged by
-- the daily retention pass (REVA_OPS_EVENTS_RETENTION_DAYS, default 30).
-- Mirrors reva/db/models.py::OpsEvent.
CREATE TABLE IF NOT EXISTS ops_events (
    id BIGSERIAL PRIMARY KEY,
    component TEXT NOT NULL,
    severity TEXT NOT NULL,
    event TEXT NOT NULL,
    detail JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ops_events_created_at ON ops_events (created_at);
CREATE INDEX IF NOT EXISTS idx_ops_events_component ON ops_events (component);
```

- [x] **Step 4: Add the ORM model**

In `reva/db/models.py`, after `ClaudeSpend`:

```python
# ------------------------------------------------------------------ ops_events


class OpsEvent(Base):
    """A caught-and-degraded component error (mirrors db/migrations/027).

    Invariant (CLAUDE.md): any error a component catches and degrades around
    must both log and record one of these — that's what makes silent
    degradation visible in the TUI Failures tab.
    """

    __tablename__ = "ops_events"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    component: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)  # warning|error
    event: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[Any | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_ops_events_created_at", "created_at"),
        Index("idx_ops_events_component", "component"),
    )
```

- [x] **Step 5: Add the writers**

In `reva/db/writers.py` (add `OpsEvent` to the models import), new section next to the other purge functions:

```python
# ------------------------------------------------------------------ ops events


def record_ops_event(
    db: Database,
    component: str,
    severity: str,
    event: str,
    detail: dict | None = None,
) -> None:
    """Persist a caught-and-degraded component error (ops-event spec §6).

    SAFE-TO-FAIL BY CONTRACT: this is called from inside degradation paths —
    an ops-log write must never break the operation it observes, so every
    failure here is swallowed and logged.
    """
    try:
        with db.session() as s:
            s.add(OpsEvent(
                component=component, severity=severity, event=event, detail=detail,
            ))
    except Exception:
        logger.warning(
            "ops_event_write_failed", component=component, event=event, exc_info=True
        )


def purge_old_ops_events(db: Database, older_than_days: int) -> int:
    """Delete ops_events older than the retention window. Returns rows deleted."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    with db.session() as s:
        result = s.execute(delete(OpsEvent).where(OpsEvent.created_at < cutoff))
        return result.rowcount
```

(`logger`, `delete`, `datetime`/`timedelta`/`timezone` already exist in writers.py — verify with `grep -n "^logger\|^from sqlalchemy import\|^from datetime import" reva/db/writers.py` and extend imports only if missing.)

- [x] **Step 6: Run to verify pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ops_events.py tests/test_db.py -q`
Expected: PASS

- [x] **Step 7: Commit**

```bash
git add db/migrations/027_ops_events.sql reva/db/models.py reva/db/writers.py worker/tests/test_ops_events.py
git commit -m "feat(db): ops_events table + safe-to-fail writer"
```

---

### Task 2: Retention wiring + .env.example

**Files:**
- Modify: `scheduler/scheduler/settings.py`, `scheduler/scheduler/main.py` (`maybe_purge_ticket_text`), `.env.example`
- Test: `scheduler/tests/test_settings.py` (append; if the assertion style differs, mirror the file's existing tests)

**Interfaces:**
- Consumes: `writers.purge_old_ops_events` (Task 1).
- Produces: `Settings.ops_events_retention_days: int = 30` (env `REVA_OPS_EVENTS_RETENTION_DAYS`).

- [x] **Step 1: Write the failing test**

Append to `scheduler/tests/test_settings.py`:

```python
def test_ops_events_retention_default(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    from scheduler.settings import Settings

    assert Settings.from_env().ops_events_retention_days == 30

    monkeypatch.setenv("REVA_OPS_EVENTS_RETENTION_DAYS", "7")
    assert Settings.from_env().ops_events_retention_days == 7
```

- [x] **Step 2: Run to verify failure**

Run: `cd scheduler && .venv/bin/python -m pytest tests/test_settings.py -q`
Expected: FAIL — `AttributeError: ops_events_retention_days`

- [x] **Step 3: Wire settings + purge**

`scheduler/scheduler/settings.py` — after `retention_purge_interval_seconds`:

```python
    # Ops-event log retention (spec §6): degradation events older than this
    # are deleted by the daily retention pass.
    ops_events_retention_days: int = 30
```

and in `from_env`:

```python
            ops_events_retention_days=int(
                os.environ.get("REVA_OPS_EVENTS_RETENTION_DAYS", "30")
            ),
```

`scheduler/scheduler/main.py` — extend `maybe_purge_ticket_text` with a defaulted param (existing callers/tests unaffected):

```python
def maybe_purge_ticket_text(db, now, last_purge, interval_s, retention_days,
                            ops_events_retention_days: int = 30):
```

add before its `return now`:

```python
    # Ops-event log: same daily cadence (spec §6).
    purged_ops = writers.purge_old_ops_events(db, ops_events_retention_days)
    if purged_ops:
        logger.info("ops_events_purged", rows=purged_ops,
                    retention_days=ops_events_retention_days)
```

and pass it at the call site in `main()`:

```python
            last_purge = maybe_purge_ticket_text(
                db, now, last_purge, settings.retention_purge_interval_seconds,
                settings.ticket_text_retention_days,
                settings.ops_events_retention_days,
            )
```

(If the hardening batch's Task 11 landed first, this call site already has a
`spend_retention_days` argument — append `ops_events_retention_days` after it
and give the function both defaulted params.)

`.env.example` — add under the retention/optional section:

```bash
# REVA_OPS_EVENTS_RETENTION_DAYS=30    # delete component-degradation events after N days
```

- [x] **Step 4: Run to verify pass**

Run: `cd scheduler && .venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add scheduler/scheduler/settings.py scheduler/scheduler/main.py .env.example scheduler/tests/test_settings.py
git commit -m "feat(retention): purge ops_events on the daily pass"
```

---

### Task 3: Worker hooks — CodeGraph, git, Odoo callbacks

**Files:**
- Modify: `reva/claude_code_runner.py` (constructor, `_codegraph_prepare`, `_run_git`), `worker/worker/runner.py` (`build_worker_context`), `worker/worker/ticket_runner.py` (callback except), `worker/worker/ticket_issue_runner.py` (`_send_failed_callback` except)
- Test: `worker/tests/test_ops_hooks.py`

**Interfaces:**
- Consumes: `writers.record_ops_event` (Task 1).
- Produces: `ClaudeCodeRunner(…, ops_recorder: Callable[[str, str, str, dict], None] | None = None)`; internal helper `self._record_ops(component, severity, event, detail)`.

- [x] **Step 1: Write the failing tests**

Create `worker/tests/test_ops_hooks.py`:

```python
"""Degradation paths must record ops events (spec §6 hook points)."""

from __future__ import annotations

import subprocess

import pytest

from reva.claude_code_runner import ClaudeCodeRunner
from reva.errors import TransientError


def _runner(tmp_path, events):
    return ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path), api_key="k",
        skills_dir=str(tmp_path), prompts_dir=str(tmp_path),
        codegraph_enabled=True,
        ops_recorder=lambda c, s, e, d: events.append((c, s, e, d)),
    )


def test_codegraph_failure_records_event(tmp_path, monkeypatch):
    events: list = []
    runner = _runner(tmp_path, events)

    def boom(*args, **kwargs):
        raise FileNotFoundError("codegraph not installed")

    monkeypatch.setattr("reva.claude_code_runner.subprocess.run", boom)
    assert runner._codegraph_prepare(str(tmp_path)) is None

    assert events, "CodeGraph fallback must record an ops event"
    component, severity, event, detail = events[0]
    assert component == "codegraph"
    assert severity == "warning"
    assert event == "index_skipped"
    assert "codegraph not installed" in detail["error"]


def test_codegraph_nonzero_exit_records_event(tmp_path, monkeypatch):
    events: list = []
    runner = _runner(tmp_path, events)

    class R:
        returncode = 1
        stderr = "parse explosion"
        stdout = ""

    monkeypatch.setattr(
        "reva.claude_code_runner.subprocess.run", lambda *a, **k: R()
    )
    assert runner._codegraph_prepare(str(tmp_path)) is None
    assert events[0][2] == "index_failed"
    assert "parse explosion" in events[0][3]["error"]


def test_git_timeout_records_event(tmp_path, monkeypatch):
    events: list = []
    runner = _runner(tmp_path, events)

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr("reva.claude_code_runner.subprocess.run", slow)
    with pytest.raises(TransientError):
        runner._run_git(["-C", str(tmp_path), "fetch", "origin"], TransientError)
    assert events[0][0] == "git"
    assert events[0][2] == "timeout"
    assert events[0][3]["cmd"] == "fetch"


def test_no_recorder_is_safe(tmp_path, monkeypatch):
    """ops_recorder=None (tests, fixtures) must not break anything."""
    runner = ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path), api_key="k",
        skills_dir=str(tmp_path), prompts_dir=str(tmp_path),
        codegraph_enabled=True,
    )

    def boom(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr("reva.claude_code_runner.subprocess.run", boom)
    assert runner._codegraph_prepare(str(tmp_path)) is None  # no exception


def test_recorder_exception_is_swallowed(tmp_path, monkeypatch):
    def bad_recorder(c, s, e, d):
        raise RuntimeError("recorder broken")

    runner = ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path), api_key="k",
        skills_dir=str(tmp_path), prompts_dir=str(tmp_path),
        codegraph_enabled=True, ops_recorder=bad_recorder,
    )

    def boom(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr("reva.claude_code_runner.subprocess.run", boom)
    assert runner._codegraph_prepare(str(tmp_path)) is None  # no exception
```

- [x] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ops_hooks.py -q`
Expected: FAIL — `TypeError: unexpected keyword argument 'ops_recorder'`

- [x] **Step 3: Extend `ClaudeCodeRunner`**

Constructor (locate the `__init__` parameter list — it currently ends with `codegraph_index_timeout`): add the parameter and assignment:

```python
        ops_recorder: "Callable[[str, str, str, dict], None] | None" = None,
```
```python
        self.ops_recorder = ops_recorder
```

(add `from collections.abc import Callable` to the imports if absent). Add the helper next to `_subprocess_env`:

```python
    def _record_ops(self, component: str, severity: str, event: str, detail: dict) -> None:
        """Forward a degradation to the injected ops recorder (spec §6).

        The runner is deliberately DB-free; the worker injects a closure over
        writers.record_ops_event. Never raises — the observer must not break
        the observed operation.
        """
        if self.ops_recorder is None:
            return
        try:
            self.ops_recorder(component, severity, event, detail)
        except Exception:
            logger.warning("ops_recorder_failed", event=event, exc_info=True)
```

In `_codegraph_prepare`, extend both failure branches:

```python
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("codegraph_index_skipped", repo=repo_path, error=str(exc))
            self._record_ops("codegraph", "warning", "index_skipped",
                             {"repo": repo_path, "error": str(exc)})
            return None
        if result.returncode != 0:
            logger.warning(
                "codegraph_index_failed", repo=repo_path, stderr=(result.stderr or "")[:200]
            )
            self._record_ops("codegraph", "warning", "index_failed",
                             {"repo": repo_path, "error": (result.stderr or "")[:200]})
            return None
```

In `_run_git`, extend the timeout branch (the transient one — permanent git failures already surface as failed runs):

```python
        except subprocess.TimeoutExpired as exc:
            # A timeout is always transient (network/load), regardless of the
            # caller's error_class — retrying is the right move, and it must not
            # be allowed to hang forever under the per-repo lock.
            self._record_ops("git", "warning", "timeout",
                             {"cmd": cmd, "timeout_s": _GIT_TIMEOUT})
            raise TransientError(
                f"git {cmd} timed out after {_GIT_TIMEOUT}s"
            ) from exc
```

- [x] **Step 4: Wire the recorder + the two Odoo-callback hooks**

`worker/worker/runner.py::build_worker_context` — the runner is constructed after `db` exists; add the recorder argument to the existing `ClaudeCodeRunner(...)` call:

```python
        ops_recorder=lambda c, s, e, d: writers.record_ops_event(db, c, s, e, d),
```

`worker/worker/ticket_runner.py` — in the callback `except (PermanentError, TransientError):` block (currently just logs + raises), add before `raise`:

```python
        writers.record_ops_event(ctx.db, "odoo_callback", "error", "write_field_failed", {
            "analysis_id": params.analysis_id, "ticket_id": params.ticket_id,
        })
```

`worker/worker/ticket_issue_runner.py::_send_failed_callback` — in its `except Exception:` block, add after the existing `log.warning(...)`:

```python
        writers.record_ops_event(ctx.db, "odoo_callback", "error",
                                 "issues_failed_callback_error",
                                 {"run_id": params.run_id, "ticket_id": params.ticket_id})
```

- [x] **Step 5: Run to verify pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ops_hooks.py tests/test_claude_code_runner.py tests/test_ticket_runner.py tests/test_ticket_issue_runner.py tests/test_runner.py -q`
Expected: PASS (the new constructor param defaults to None, so existing fixtures are unaffected)

- [x] **Step 6: Commit**

```bash
git add reva/claude_code_runner.py worker/worker/runner.py worker/worker/ticket_runner.py worker/worker/ticket_issue_runner.py worker/tests/test_ops_hooks.py
git commit -m "feat(worker): record ops events on CodeGraph/git/callback degradations"
```

---

### Task 4: API — `GET /api/v1/ops-events` + dashboard counter

**Files:**
- Create: `api/app/schemas/ops_events.py`, `api/app/queries/ops_events.py`, `api/app/routes/v1/ops_events.py`
- Modify: `api/app/routes/v1/__init__.py` (import + `_master.include_router`), `api/app/queries/metrics.py` (`dashboard_metrics`), `api/app/schemas/metrics.py` (`DashboardMetrics`)
- Test: `api/tests/test_v1_ops_events.py`

**Interfaces:**
- Consumes: `OpsEvent` model, `record_ops_event` (Task 1).
- Produces: `GET /api/v1/ops-events?component=&severity=&limit=&offset=` (master key) returning `{items: [{id, component, severity, event, detail, created_at}], total}`; `DashboardMetrics.degradations_24h: int` (count of ops_events in the last 24h).

- [x] **Step 1: Write the failing tests**

Create `api/tests/test_v1_ops_events.py`:

```python
"""GET /api/v1/ops-events + the dashboard degradation counter."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers


@pytest.fixture()
def client_db():
    engine = create_engine_from_url(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    # The dashboard route depends on get_redis → app.state.rq_queue.connection;
    # give it a stub (._count_workers treats a dead connection as 0 workers —
    # mirror whatever the existing tests/test_v1_metrics.py fixture does here
    # if it differs).
    prev_queue = getattr(app.state, "rq_queue", None)
    app.state.rq_queue = type("Q", (), {"connection": None})()
    yield TestClient(app), db
    app.state.rq_queue = prev_queue
    app.dependency_overrides.clear()


def _seed(db):
    writers.record_ops_event(db, "codegraph", "warning", "index_failed",
                             {"repo": "acme/widgets"})
    writers.record_ops_event(db, "odoo_callback", "error", "write_field_failed",
                             {"analysis_id": 7})


def test_list_all(client_db):
    client, db = client_db
    _seed(db)
    r = client.get("/api/v1/ops-events")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    # Newest first.
    assert body["items"][0]["event"] == "write_field_failed"
    assert body["items"][0]["detail"] == {"analysis_id": 7}


def test_filters(client_db):
    client, db = client_db
    _seed(db)
    assert client.get("/api/v1/ops-events?component=codegraph").json()["total"] == 1
    assert client.get("/api/v1/ops-events?severity=error").json()["total"] == 1
    assert client.get("/api/v1/ops-events?component=nope").json()["total"] == 0


def test_dashboard_degradations_counter(client_db):
    client, db = client_db
    _seed(db)
    r = client.get("/api/v1/metrics/dashboard")
    assert r.status_code == 200
    assert r.json()["degradations_24h"] == 2
```

- [x] **Step 2: Run to verify failure**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_ops_events.py -q`
Expected: FAIL — 404 on `/api/v1/ops-events`

- [x] **Step 3: Schemas, query, route**

`api/app/schemas/ops_events.py`:

```python
"""Pydantic schemas for the ops-event endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class OpsEventEntry(BaseModel):
    id: int
    component: str
    severity: str
    event: str
    detail: dict | None
    created_at: datetime


class OpsEventPage(BaseModel):
    items: list[OpsEventEntry]
    total: int
```

`api/app/queries/ops_events.py`:

```python
"""Read queries for the ops-event log."""

from __future__ import annotations

from sqlalchemy import func, select

from reva.db.engine import Database
from reva.db.models import OpsEvent


def list_ops_events(
    db: Database,
    component: str | None = None,
    severity: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return (items, total), newest first, optionally filtered."""
    with db.session() as s:
        base = select(OpsEvent)
        count_q = select(func.count()).select_from(OpsEvent)
        if component:
            base = base.where(OpsEvent.component == component)
            count_q = count_q.where(OpsEvent.component == component)
        if severity:
            base = base.where(OpsEvent.severity == severity)
            count_q = count_q.where(OpsEvent.severity == severity)

        total = s.execute(count_q).scalar_one()
        rows = s.execute(
            base.order_by(OpsEvent.created_at.desc(), OpsEvent.id.desc())
            .limit(limit).offset(offset)
        ).scalars().all()
        items = [
            {
                "id": r.id,
                "component": r.component,
                "severity": r.severity,
                "event": r.event,
                "detail": r.detail,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    return items, total
```

`api/app/routes/v1/ops_events.py`:

```python
"""Ops-event log endpoints (component degradations, spec §6)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import get_db
from app.pagination import clamp_limit, clamp_offset
from app.queries import ops_events as q
from app.schemas.ops_events import OpsEventEntry, OpsEventPage
from reva.db.engine import Database

router = APIRouter()


@router.get("/ops-events", response_model=OpsEventPage)
def list_ops_events(
    component: str | None = None,
    severity: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Database = Depends(get_db),
) -> dict:
    """Component-degradation events, newest first."""
    limit = clamp_limit(limit, 200)
    offset = clamp_offset(offset)
    items, total = q.list_ops_events(
        db, component=component, severity=severity, limit=limit, offset=offset
    )
    return {"items": [OpsEventEntry.model_validate(i) for i in items], "total": total}
```

`api/app/routes/v1/__init__.py`: add `ops_events` to the import list and `_master.include_router(ops_events.router)` after the `odoo_instances` line.

- [x] **Step 4: Dashboard counter**

`api/app/queries/metrics.py` — add `OpsEvent` to the models import; inside `dashboard_metrics`'s `with db.session() as s:` block add:

```python
        degradations_24h = s.execute(
            select(func.count()).select_from(OpsEvent)
            .where(OpsEvent.created_at >= since_24h)
        ).scalar_one()
```

and add to the returned dict:

```python
        "degradations_24h": int(degradations_24h),
```

`api/app/schemas/metrics.py::DashboardMetrics` — add:

```python
    # Component degradations (ops_events) in the last 24h — a quietly-degrading
    # system shows here even when every run "succeeds".
    degradations_24h: int = 0
```

- [x] **Step 5: Run to verify pass**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_ops_events.py tests/test_v1_metrics.py -q`
Expected: PASS

- [x] **Step 6: Commit**

```bash
git add api/app/schemas/ops_events.py api/app/queries/ops_events.py api/app/routes/v1/ops_events.py api/app/routes/v1/__init__.py api/app/queries/metrics.py api/app/schemas/metrics.py api/tests/test_v1_ops_events.py
git commit -m "feat(api): /api/v1/ops-events + dashboard degradations counter"
```

---

### Task 5: TUI — Failures tab second view + dashboard line

**Files:**
- Modify: `tui/internal/api/types.go` (2 types + 1 field), `tui/internal/api/iface.go`, `tui/internal/api/client.go`, `tui/internal/api/mock.go`, `tui/internal/ui/messages.go`, `tui/internal/ui/failures.go`, `tui/internal/ui/app.go` (message case + statusBar hint), `tui/internal/ui/dashboard.go`
- Test: `tui/internal/ui/failures_test.go` (new)

**Interfaces:**
- Consumes: Task 4's endpoints.
- Produces: `ClientIface.OpsEvents(limit int) (*OpsEventPage, error)`; Failures tab key **`v`** toggles "failed runs" ⇄ "component events".

- [x] **Step 1: Write the failing test**

Create `tui/internal/ui/failures_test.go`:

```go
package ui

import (
	"strings"
	"testing"
	"time"

	"reva-tui/internal/api"
)

func TestFailuresOpsEventsView(t *testing.T) {
	f := newFailures(&api.MockClient{})
	detail := map[string]any{"repo": "acme/widgets"}
	page := &api.OpsEventPage{
		Items: []api.OpsEventEntry{
			{ID: 2, Component: "codegraph", Severity: "warning",
				Event: "index_failed", Detail: detail,
				CreatedAt: time.Now().Add(-5 * time.Minute)},
			{ID: 1, Component: "odoo_callback", Severity: "error",
				Event: "write_field_failed",
				CreatedAt: time.Now().Add(-2 * time.Hour)},
		},
		Total: 2,
	}
	f, _ = f.update(opsEventsLoadedMsg{data: page})
	f.width, f.height = 120, 30

	// Default view is failed runs.
	if strings.Contains(f.view(120, 30), "codegraph") {
		t.Fatal("runs view must not render ops events")
	}
	// Toggle to events view.
	f.showEvents = true
	out := f.view(120, 30)
	if !strings.Contains(out, "codegraph") || !strings.Contains(out, "index_failed") {
		t.Fatalf("events view missing rows:\n%s", out)
	}
	if !strings.Contains(out, "Component Events") {
		t.Fatalf("events view missing header:\n%s", out)
	}
}
```

- [x] **Step 2: Run to verify failure**

Run: `cd tui && go test ./internal/ui/ -run TestFailuresOpsEventsView`
Expected: FAIL — `undefined: opsEventsLoadedMsg` / unknown field `showEvents`

- [x] **Step 3: API client plumbing**

`tui/internal/api/types.go` — after the `PendingPage` block (anywhere top-level):

```go
type OpsEventEntry struct {
	ID        int            `json:"id"`
	Component string         `json:"component"`
	Severity  string         `json:"severity"`
	Event     string         `json:"event"`
	Detail    map[string]any `json:"detail"`
	CreatedAt time.Time      `json:"created_at"`
}

type OpsEventPage struct {
	Items []OpsEventEntry `json:"items"`
	Total int             `json:"total"`
}
```

and add to `DashboardMetrics`:

```go
	Degradations24h    int           `json:"degradations_24h"`
```

`tui/internal/api/iface.go` — after `Failures(...)`:

```go
	OpsEvents(limit int) (*OpsEventPage, error)
```

`tui/internal/api/client.go` — after `Failures`:

```go
func (c *Client) OpsEvents(limit int) (*OpsEventPage, error) {
	var p OpsEventPage
	return &p, c.get(fmt.Sprintf("/ops-events?limit=%d", limit), &p)
}
```

`tui/internal/api/mock.go` — anywhere among the mock methods:

```go
func (m *MockClient) OpsEvents(limit int) (*OpsEventPage, error) {
	now := time.Now()
	items := []OpsEventEntry{
		{ID: 3, Component: "codegraph", Severity: "warning", Event: "index_failed",
			Detail: map[string]any{"repo": "acme/odoo-modules"},
			CreatedAt: now.Add(-10 * time.Minute)},
		{ID: 2, Component: "odoo_callback", Severity: "error", Event: "write_field_failed",
			Detail: map[string]any{"analysis_id": 12}, CreatedAt: now.Add(-1 * time.Hour)},
		{ID: 1, Component: "git", Severity: "warning", Event: "timeout",
			Detail: map[string]any{"cmd": "fetch"}, CreatedAt: now.Add(-3 * time.Hour)},
	}
	n := limit
	if n > len(items) {
		n = len(items)
	}
	return &OpsEventPage{Items: items[:n], Total: len(items)}, nil
}
```

`tui/internal/ui/messages.go`:

```go
type opsEventsLoadedMsg struct {
	data *api.OpsEventPage
	err  error
}
```

- [x] **Step 4: Failures tab toggle**

In `tui/internal/ui/failures.go`:

Struct — add fields:

```go
	// Ops-events second view (spec §6): `v` toggles failed runs ⇄ component events.
	showEvents  bool
	events      []api.OpsEventEntry
	eventsTotal int
	eventsErr   error
```

`load()` — batch both fetches (the tickets-tab pattern):

```go
func (f Failures) load() tea.Cmd {
	client := f.client
	return tea.Batch(
		func() tea.Msg {
			data, err := client.Failures(50)
			return failuresLoadedMsg{data: data, err: err}
		},
		func() tea.Msg {
			data, err := client.OpsEvents(100)
			return opsEventsLoadedMsg{data: data, err: err}
		},
	)
}
```

`update()` — new message case + toggle key:

```go
	case opsEventsLoadedMsg:
		f.eventsErr = m.err
		if m.data != nil {
			f.events = m.data.Items
			f.eventsTotal = m.data.Total
		}
```

and in the `tea.KeyMsg` switch (next to `"r"`):

```go
		case "v":
			f.showEvents = !f.showEvents
			f.cursor, f.offset = 0, 0
			return f, nil
```

`view()` — first line becomes a dispatch:

```go
func (f Failures) view(w, h int) string {
	if f.showEvents {
		return f.eventsView(w, h)
	}
	// … existing body unchanged …
```

New method at the bottom of the file:

```go
func (f Failures) eventsView(w, h int) string {
	header := styleTitle.Padding(0, 1).Render(
		fmt.Sprintf("Component Events (%d)", f.eventsTotal))
	if f.eventsErr != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			styleStatusFailed.Render("  Error: "+f.eventsErr.Error()))
	}
	if len(f.events) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No component degradations — all good")),
			styleSubtitle.Render("  [v] back to failed runs"))
	}

	visibleRows := h - 5
	if visibleRows < 1 {
		visibleRows = 1
	}
	colSev, colComp, colEvent, colWhen := 8, 16, 28, 10
	colDetail := w - colSev - colComp - colEvent - colWhen - 12

	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s",
			colSev, "Severity", colComp, "Component", colEvent, "Event",
			colWhen, "When", colDetail, "Detail"))
	rows := []string{hdr}

	end := visibleRows
	if end > len(f.events) {
		end = len(f.events)
	}
	for _, e := range f.events[:end] {
		detail := ""
		for k, v := range e.Detail {
			detail += fmt.Sprintf("%s=%v ", k, v)
		}
		sev := e.Severity
		if e.Severity == "error" {
			sev = styleStatusFailed.Render("error   ")
		}
		rows = append(rows, fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s",
			colSev, sev,
			colComp, truncate(e.Component, colComp),
			colEvent, truncate(e.Event, colEvent),
			colWhen, relativeTime(e.CreatedAt),
			colDetail, truncate(strings.TrimSpace(detail), colDetail)))
	}
	footer := styleSubtitle.Render("  [v] back to failed runs   [r] refresh") +
		cappedNote(end, f.eventsTotal)
	return lipgloss.JoinVertical(lipgloss.Left, header, "",
		strings.Join(rows, "\n"), "", footer)
}
```

`tui/internal/ui/app.go` — add the message case next to `failuresLoadedMsg`:

```go
	case opsEventsLoadedMsg:
		a.failures, _ = a.failures.update(msg)
```

and update the Failures statusBar hint:

```go
	case viewFailures:
		hint = "j/k navigate | v=runs/events | e=requeue | r=refresh | q quit"
```

- [x] **Step 5: Dashboard line**

`tui/internal/ui/dashboard.go::renderCostCard` — after the Workers lines:

```go
	if m.Degradations24h > 0 {
		b.WriteString(fmt.Sprintf("  Degrade %s\n",
			styleStatusFailed.Render(fmt.Sprintf("%d events (24h)", m.Degradations24h))))
	}
```

- [x] **Step 6: Build, vet, test**

Run: `cd tui && go build ./... && go vet ./... && go test ./...`
Expected: PASS (compiler enforces the `ClientIface` change on `mock.go`)

- [x] **Step 7: Commit**

```bash
git add tui/internal/api/types.go tui/internal/api/iface.go tui/internal/api/client.go tui/internal/api/mock.go tui/internal/ui/messages.go tui/internal/ui/failures.go tui/internal/ui/app.go tui/internal/ui/dashboard.go tui/internal/ui/failures_test.go
git commit -m "feat(tui): component-events view on Failures tab + dashboard counter"
```

---

### Task 6: CLAUDE.md invariant + final verification

**Files:**
- Modify: `CLAUDE.md` (invariants list)

- [x] **Step 1: Add the invariant**

In `CLAUDE.md`, in the "Invariants the design leans on" list (after the "Untrusted content is fenced" bullet), add:

```markdown
- **Degradations are visible.** Any error a component catches and degrades around (CodeGraph fallback, callback failure, retrieval miss, git retry) must both log AND `writers.record_ops_event(...)` — surfaced via `GET /api/v1/ops-events` and the TUI Failures tab. Silent `except: log-and-continue` without an ops event is a review-blocking defect in new code.
```

- [x] **Step 2: Full Definition of Done**

```bash
make test
ruff check reva worker/worker api/app scheduler/scheduler
cd tui && go build ./... && go vet ./... && go test ./... && cd ..
```
Expected: all green. Run `make test-integration` if Docker is available (JSONB + migration SQL on real Postgres).

- [x] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: ops-event invariant — degraded errors must be recorded"
```
