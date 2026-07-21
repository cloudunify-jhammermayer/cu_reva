# Monthly Value Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** A persisted monthly per-repo/per-instance rollup of REVA's delivered value (findings, confirmed-fixed rate, spend, throughput) — generated always, sent to Chat only when explicitly enabled (default OFF).

**Architecture:** Scheduler fires monthly → worker job runs pure DB rollup queries → markdown report persisted in a `value_reports` row (unique per period, re-runs replace) → optional Chat summary behind `REVA_VALUE_REPORT_CHAT_ENABLED` → `GET /api/v1/value-reports` for manual review. No LLM call anywhere.

**Tech Stack:** Python 3.14, existing scheduler/worker/notifier rails. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-05-value-report-design.md`.

## Global Constraints

- **Chat delivery default OFF** (`REVA_VALUE_REPORT_CHAT_ENABLED=false`) — Joseph reviews persisted reports manually first. Generation/persistence always runs.
- Pure queries only — no Claude call, no new data collection.
- **Migration number:** next free on disk. TUI surface deliberately deferred (spec) — do not add one.
- Final gate: `make test` + `worker/.venv/bin/ruff check reva worker/worker api/app scheduler/scheduler`.

---

### Task 1: DB + builder

**Files:**
- Create: `db/migrations/0NN_value_reports.sql`, `reva/value_report.py`
- Modify: `reva/db/models.py` (`ValueReport`), `reva/db/writers.py` (`upsert_value_report`, `get_value_reports`)
- Test: `worker/tests/test_value_report.py`

**Interfaces:**
- Produces:
  - `ValueReport(id, period_start, period_end, content_md, stats, chat_sent, created_at)`; unique `(period_start, period_end)`
  - `writers.upsert_value_report(db, period_start, period_end, content_md, stats: dict) -> int` (replace on re-run, resets `chat_sent=False`)
  - `writers.set_value_report_chat_sent(db, report_id) -> None`
  - `writers.get_value_reports(db, limit=12) -> list[dict]` (newest first, full content)
  - `reva.value_report.build_report(db, period_start, period_end) -> tuple[str, dict]` — `(markdown, stats)`; always returns a report, even for an empty month

- [x] **Step 1: Write the failing tests**

Create `worker/tests/test_value_report.py`:

```python
"""Monthly value report: rollups, empty months, upsert-replace (spec)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.value_report import build_report

_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
_END = datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _seed(db):
    """One completed review with findings in June + one outside the period.

    Uses the same ORM seeding style as test_v1_metrics/test_db — adapt the
    exact required fields from reva/db/models.py (ReviewRun, ReviewFinding
    with outcome, Repository, PullRequest); the assertions are the contract.
    """
    from reva.db.models import (PullRequest, Repository, ReviewFinding,
                                ReviewRun)

    with db.session() as s:
        r = Repository(full_name="acme/widgets")
        s.add(r); s.flush()
        p = PullRequest(repository_id=r.id, pr_number=1)
        s.add(p); s.flush()
        inside = ReviewRun(repository_id=r.id, pull_request_id=p.id,
                           head_sha="a" * 40, review_mode="diff",
                           status="completed",
                           created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
                           estimated_cost_usd=0.20)
        outside = ReviewRun(repository_id=r.id, pull_request_id=p.id,
                            head_sha="b" * 40, review_mode="diff",
                            status="completed",
                            created_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
                            estimated_cost_usd=0.50)
        s.add_all([inside, outside]); s.flush()
        s.add(ReviewFinding(review_run_id=inside.id, file="a.py", line=1,
                            severity="major", category="bug", title="t",
                            outcome="resolved_by_fix"))
        s.add(ReviewFinding(review_run_id=inside.id, file="b.py", line=2,
                            severity="minor", category="style", title="t2",
                            outcome="dismissed"))


def test_report_contains_period_rollups(db):
    _seed(db)
    md, stats = build_report(db, _START, _END)
    assert "acme/widgets" in md
    assert stats["reviews"] == 1              # May run excluded
    assert stats["findings"] == 2
    assert stats["resolved_by_fix"] == 1
    assert stats["spend_usd"] == pytest.approx(0.20)
    assert "2026-06" in md


def test_empty_month_still_reports(db):
    md, stats = build_report(db, _START, _END)
    assert stats["reviews"] == 0
    assert "no completed reviews" in md.lower()


def test_upsert_replaces_and_resets_chat_flag(db):
    rid = writers.upsert_value_report(db, _START, _END, "v1", {"reviews": 0})
    writers.set_value_report_chat_sent(db, rid)
    rid2 = writers.upsert_value_report(db, _START, _END, "v2", {"reviews": 1})
    assert rid == rid2
    rows = writers.get_value_reports(db)
    assert len(rows) == 1
    assert rows[0]["content_md"] == "v2"
    assert rows[0]["chat_sent"] is False


def test_list_newest_first(db):
    writers.upsert_value_report(db, datetime(2026, 5, 1, tzinfo=timezone.utc),
                                _START, "may", {})
    writers.upsert_value_report(db, _START, _END, "june", {})
    rows = writers.get_value_reports(db)
    assert rows[0]["content_md"] == "june"
```

- [x] **Step 2: Run to verify failure, then implement**

Migration `0NN_value_reports.sql`:

```sql
-- Monthly value reports (spec 2026-07-05): generated always, Chat delivery
-- opt-in. One row per period; re-runs replace. Mirrors
-- reva/db/models.py::ValueReport.
CREATE TABLE IF NOT EXISTS value_reports (
    id BIGSERIAL PRIMARY KEY,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    content_md TEXT NOT NULL,
    stats JSONB,
    chat_sent BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_value_reports_period
    ON value_reports (period_start, period_end);
```

ORM model (the established shape) + writers:

```python
def upsert_value_report(db: Database, period_start, period_end,
                        content_md: str, stats: dict) -> int:
    """One row per period; a re-run replaces content and resets chat_sent."""
    with db.session() as s:
        row = s.execute(
            select(ValueReport).where(
                ValueReport.period_start == period_start,
                ValueReport.period_end == period_end,
            )
        ).scalars().first()
        if row is None:
            row = ValueReport(period_start=period_start, period_end=period_end,
                              content_md=content_md, stats=stats)
            s.add(row)
            s.flush()
        else:
            row.content_md = content_md
            row.stats = stats
            row.chat_sent = False
        return row.id


def set_value_report_chat_sent(db: Database, report_id: int) -> None:
    with db.session() as s:
        row = s.get(ValueReport, report_id)
        if row is not None:
            row.chat_sent = True


def get_value_reports(db: Database, limit: int = 12) -> list[dict]:
    with db.session() as s:
        rows = s.execute(
            select(ValueReport)
            .order_by(ValueReport.period_start.desc()).limit(limit)
        ).scalars().all()
        return [{
            "id": r.id, "period_start": r.period_start,
            "period_end": r.period_end, "content_md": r.content_md,
            "stats": r.stats, "chat_sent": r.chat_sent,
            "created_at": r.created_at,
        } for r in rows]
```

`reva/value_report.py` — pure builder: per-repo rollup (reviews by mode,
findings by severity, `resolved_by_fix`/`dismissed` counts from the
finding-outcome column, spend sum), per-instance rollup (ticket analyses,
issue runs, ready tickets via the Task-8 loop query when present — degrade
to omitting the line if the helper is absent), `claude_spend` by kind for
the period; renders markdown sections `# REVA value report 2026-06`,
`## Fleet`, `## Repositories` (table), `## Odoo instances` (table),
`## Spend by kind`; returns `(md, stats)` where stats carries the fleet
totals asserted in the tests. Empty month → a one-paragraph report saying
"no completed reviews in this period" + zeroed stats. (~120 lines of
straightforward `select`/`func.count` — mirror `api/app/queries/metrics.py`
aggregation style; the outcome column name comes from
`grep -n "outcome" reva/db/models.py`.)

- [x] **Step 3: Run to verify pass, commit**

```bash
cd worker && .venv/bin/python -m pytest tests/test_value_report.py -q
git add db/migrations/ reva/db/models.py reva/db/writers.py reva/value_report.py worker/tests/test_value_report.py
git commit -m "feat(report): monthly value-report builder + persistence"
```

---

### Task 2: Scheduler trigger + worker task + Chat gating

**Files:**
- Create: `worker/worker/value_report_tasks.py`, `worker/worker/value_report_runner.py`
- Modify: `scheduler/scheduler/settings.py`, `scheduler/scheduler/main.py`, `worker/worker/settings.py` (chat flag), `.env.example`
- Test: `worker/tests/test_value_report_runner.py`, `scheduler/tests/test_value_report_due.py`

**Interfaces:**
- Produces: scheduler settings `value_report_day: int = 1` (`REVA_VALUE_REPORT_DAY`), `value_report_hour_utc: int = 7` (`REVA_VALUE_REPORT_HOUR_UTC`); worker setting `value_report_chat_enabled: bool = False` (`REVA_VALUE_REPORT_CHAT_ENABLED`); RQ entry `"worker.value_report_tasks.run_value_report"` (params `{period_start_iso, period_end_iso}`).

- [x] **Step 1: Failing tests**

`scheduler/tests/test_value_report_due.py` — a `maybe_enqueue_value_report(queue, now, last_sent, day, hour)` helper (the `maybe_enqueue_eviction` shape): fires once when `now` is on/after (day, hour) of the month AND `last_sent` is in a previous month; params carry the PREVIOUS calendar month's ISO bounds; not due → unchanged; month rollover covered (Jan→Dec of previous year).

`worker/tests/test_value_report_runner.py` — with fakes: report built+persisted; **chat flag off (default) → notifier never called, `chat_sent` stays False**; flag on → notifier called once with the summary and `chat_sent=True`; notifier failure → report persisted, ops event (`value_report/chat_failed`), no exception; re-run same period → replaced row (no duplicate).

Write both concretely (the scheduler helper is new and pure — full code
tests; the runner test uses `Base.metadata.create_all` + a monkeypatched
`worker.value_report_runner.notify_value_report`).

- [x] **Step 2: Implement**

`worker/worker/value_report_runner.py`:

```python
"""Monthly value-report job (spec 2026-07-05). Pure DB + optional Chat."""

from __future__ import annotations

from datetime import datetime

import structlog

from reva.db import writers
from reva.notifications import notify_operational_alert
from reva.value_report import build_report
from worker.runner import get_context

logger = structlog.get_logger()


def notify_value_report(webhook_url: str, summary: str) -> None:
    """Chat delivery seam (monkeypatched in tests)."""
    notify_operational_alert(webhook_url, summary)


def run_value_report(job_params: dict) -> dict:
    ctx = get_context()
    start = datetime.fromisoformat(job_params["period_start_iso"])
    end = datetime.fromisoformat(job_params["period_end_iso"])
    md, stats = build_report(ctx.db, start, end)
    report_id = writers.upsert_value_report(ctx.db, start, end, md, stats)
    logger.info("value_report_persisted", report_id=report_id, **{
        k: stats.get(k) for k in ("reviews", "findings", "spend_usd")
    })

    if not ctx.value_report_chat_enabled:
        return {"status": "persisted", "report_id": report_id}
    try:
        summary = md.split("## Repositories")[0][:3500]
        notify_value_report(ctx.google_chat_webhook_url, summary)
        writers.set_value_report_chat_sent(ctx.db, report_id)
        return {"status": "sent", "report_id": report_id}
    except Exception as exc:
        logger.warning("value_report_chat_failed", exc_info=True)
        writers.record_ops_event(ctx.db, "value_report", "warning",
                                 "chat_failed", {"error": str(exc)[:300]})
        return {"status": "persisted_chat_failed", "report_id": report_id}
```

(Adapter: `notify_operational_alert`'s real signature —
`grep -n "def notify_operational_alert" reva/notifications.py` — wrap
accordingly; `WorkerContext` gains the defaulted field
`value_report_chat_enabled: bool = False` populated from the new worker
setting.) `value_report_tasks.py` = the `terminal_on_permanent` wrapper.

Scheduler: settings fields + `maybe_enqueue_value_report` (pure, tested) +
a call in the main loop (own try/except like the other maybes; `last_sent`
initialised from the newest persisted report? simplest: `None` at boot —
the enqueue is idempotent because the runner upserts per period, so a
restart double-fire is harmless). `.env.example`:

```bash
# --- Monthly value report (optional) --------------------------------------------
# Generated + persisted every month (GET /api/v1/value-reports). Chat delivery
# is OPT-IN — leave off until you've reviewed a few reports manually.
# REVA_VALUE_REPORT_CHAT_ENABLED=false
# REVA_VALUE_REPORT_DAY=1
# REVA_VALUE_REPORT_HOUR_UTC=7
```

- [x] **Step 3: Run to verify pass, commit**

```bash
cd worker && .venv/bin/python -m pytest tests/test_value_report_runner.py -q && cd ../scheduler && .venv/bin/python -m pytest tests/ -q
git add worker/ scheduler/ .env.example
git commit -m "feat(report): monthly trigger + chat gating (default off)"
```

---

### Task 3: API read endpoints

**Files:**
- Create: `api/app/routes/v1/value_reports.py`, `api/app/schemas/value_reports.py`
- Modify: `api/app/routes/v1/__init__.py` (import + `_master.include_router`)
- Test: `api/tests/test_v1_value_reports.py`

- [x] **Step 1: Failing tests** — list returns newest-first with full
`content_md`; `latest` 200/404; master-key gated (the standard fixture).
Write them in the established `test_v1_*` style (fixture: db override +
`Base.metadata.create_all`, seed via `writers.upsert_value_report`).
- [x] **Step 2: Implement** — schemas `ValueReportEntry`
(`id, period_start, period_end, content_md, stats, chat_sent, created_at`) +
`ValueReportPage`; routes `GET /value-reports?limit=` (clamped 24) and
`GET /value-reports/latest` (404 when none) reading via
`writers.get_value_reports`; wire into `_master`.
- [x] **Step 3: Run + commit**

```bash
cd api && .venv/bin/python -m pytest tests/test_v1_value_reports.py -q
git add api/
git commit -m "feat(api): value-report read endpoints"
```

---

### Task 4: Final verification

- [x] **Step 1:** Full DoD:

```bash
make test
worker/.venv/bin/ruff check reva worker/worker api/app scheduler/scheduler
docker compose -f docker-compose.prod.yml config -q
```

- [x] **Step 2:** Commit anything outstanding + report. State honestly:
Chat delivery ships OFF and stays off until Joseph flips
`REVA_VALUE_REPORT_CHAT_ENABLED` after manually reviewing persisted reports
(`GET /api/v1/value-reports/latest`); TUI surface deliberately deferred with
the quality dashboard; month-boundary logic is unit-tested, first real fire
happens on the next 1st.
