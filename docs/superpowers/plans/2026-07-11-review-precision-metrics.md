# Review Precision Metrics + Dismissal-Rate Alarm — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-repo rolling-30-day precision metrics (findings_posted, dismissed, fixed, dismissal_rate, addressed_rate; min-sample floor 10) computed on read; surfaced on a new `/api/v1/metrics/precision` endpoint, the TUI Feedback tab (warning styling above threshold), and a weekly-report section; plus a daily scheduler alarm (Google Chat + ops event) with a persisted 7-day per-repo dedup. Zero model calls, no new tables, no auto-behavior.

**Spec:** `docs/superpowers/specs/2026-07-11-review-precision-metrics-design.md` (approved design 2026-07-11).

**Architecture — resolved data-model mapping (the spec deferred this to planning):**

| Metric | Persisted source |
|---|---|
| `findings_posted` | `review_findings` rows with `posted_to_github = TRUE`, window on `review_findings.created_at >= now − 30d`, repo via `review_runs.repository_id → repositories.full_name`. The mute-drop and 15-cap happen in `Reviewer.execute()` *before* persistence, and `posted_to_github` is set only by `writers.attach_finding_comment_ids` (`reva/db/writers.py:864`, called from the worker's `_backfill_comment_ids`, `worker/worker/runner.py:795`) for findings that actually landed as inline comments — so this filter IS "posted inline, post-cap/post-mute". Same source of truth `/dismiss` matching uses. |
| `dismissed` | distinct `review_feedback.review_finding_id` with `reaction = 'dismissed'` — the exact row the `/dismiss` command writes (`api/app/routes/webhooks.py:301-315`). **Not** `is_positive = FALSE` (that would also count 👎 reactions and thread-unresolved signals; the spec says explicit `/dismiss`). A finding dismissed by two reactors counts once (distinct finding ids). Dismissals are structurally a subset of posted findings (a `/dismiss` replies to an inline comment). |
| `fixed` | `review_findings.outcome = 'resolved_by_fix'` (the migration-015 outcome ledger, set by `writers.set_finding_outcome` from the delta-review resolution pass). `still_open_at_merge` / `open` remain unaddressed context — no third rate, per spec. |
| alarm dedup | the recorded ops event itself: `ops_events(component='feedback', event='dismissal_rate_high', detail->>'repo')` looked back 7 days. The spec's "persisted marker, same dedup approach as the weekly report" would need a table like `weekly_reports` — but the spec also forbids new tables, so the ops event doubles as the marker. Safe: `REVA_OPS_EVENTS_RETENTION_DAYS` defaults to 30 (> 7). |

**Spec corrections discovered during planning** (Task 6 records them in the spec):
- The spec says ops event `kind="dismissal_rate_high"`; the `ops_events` column and `record_ops_event(db, component, severity, event, detail)` parameter is **`event`** (`reva/db/writers.py:1377`, `reva/db/models.py:818`). Mapping: `component="feedback"`, `severity="warning"`, `event="dismissal_rate_high"`, `detail={"repo": ...}` (the `"repo"` detail key matches the existing board_status convention).
- The spec says "add per-repo dismissed%/fixed% columns next to the existing signal counts" — the Feedback tab's existing table is per **(repo, category)** (`GET /metrics/learning`); repo-level rates can't be columns on category rows without repeating values. Implemented as a per-repo summary table rendered above the per-category table, from a new sibling endpoint `GET /metrics/precision` in the same metrics router the tab already reads.

**Tech Stack:** Python 3.14 (SQLAlchemy aggregates in shared `reva/db/writers.py`, FastAPI + pydantic response models, scheduler loop function), Go/Bubble Tea TUI.

## Global Constraints

- **Five plans execute as one wave.** `reva/types.py` `RepoConfig`: sibling plans add keys (`commit_suggestions`, `describe_command`, `fix_command`, `issue_plans`); **THIS plan adds NO RepoConfig key.**
- **Env addition (THIS plan only):** `REVA_DISMISSAL_RATE_ALERT` (default `0.30`, `0` disables) in `reva/config.py` + README env-table row, following the existing alert-threshold pattern. `.env.example` must gain the row too — `worker/tests/test_env_example.py` scans `reva/config.py` and gates it.
- **`prompts/CHANGELOG.md` is NOT touched by this plan** (no prompt changes).
- **No changes to `reva/odoo_contracts.py` or `contracts/`. No DB migrations, NO new tables** (read-side aggregation only; window is small — 30 days, bounded like `feedback_stats`'s PERF-5 cutoff).
- **Ops-event invariant (CLAUDE.md):** any caught-and-degraded path must log AND `writers.record_ops_event(...)`. The alarm itself records `component="feedback"`, `kind="dismissal_rate_high"` per spec — persisted in the `event` column (see mapping above).
- **This plan touches `tui/`** → final verification includes `cd tui && go build ./... && go vet ./... && go test ./...` and `gofmt -l tui/` printing nothing, plus TUI mock/demo client coverage so demo mode (`go run . --demo`) shows the new columns (house rule).
- **Min-sample floor is 10**, a keyword default on the metric function — not an env var (only `REVA_DISMISSAL_RATE_ALERT` is configurable).
- `reva/` is shared by all three services: final verification is `make test`, `worker/.venv/bin/ruff check reva worker/worker api/app scheduler/scheduler`, plus the Go gate above.
- Per-service venvs: `cd worker && .venv/bin/python -m pytest tests/...` (same pattern for `api/` and `scheduler/`).

---

### Task 0: Preconditions

- [ ] **Step 1: Verify the working tree, base commit, and spec**

```bash
git status --porcelain
git merge-base --is-ancestor e10364b HEAD && echo BASE_OK
test -f docs/superpowers/specs/2026-07-11-review-precision-metrics-design.md && echo SPEC_OK
test -x worker/.venv/bin/python && test -x api/.venv/bin/python && test -x scheduler/.venv/bin/python && echo VENVS_OK
```

Expected: no output from `git status --porcelain` (clean tree); `BASE_OK`; `SPEC_OK`; `VENVS_OK`. If the tree is dirty or the base commit is missing, STOP and report — do not proceed.

---

### Task 1: Metric definition in shared writers + threshold config + env docs

**Files:**
- Modify: `reva/db/writers.py` (add `get_repo_precision_stats` after `get_memory_distill_input`, i.e. before the `# --- ticket_analyses writers` header ~line 1211; add `repos_with_recent_ops_event` after `purge_old_ops_events` ~line 1408)
- Modify: `reva/config.py` (add `DISMISSAL_RATE_ALERT` after `VERIFY_MODEL`, line 21)
- Modify: `.env.example` (alert section, after `REVA_REPO_CACHE_DISK_PCT_ALERT` line 94)
- Modify: `README.md` (env table — add a row after the `REVA_QUEUE_DEPTH_ALERT / ...` row, line 272)
- Create: `worker/tests/test_precision_stats.py`

**Interfaces:**
- Produces: `writers.get_repo_precision_stats(db, *, window_days=30, min_findings=10, dismissal_alert=0.30) -> list[dict]` — rows `{"repo", "findings_posted", "dismissed", "fixed", "dismissal_rate", "addressed_rate", "flagged"}`, sorted by repo. Consumed by Tasks 2 (API), 3 (weekly report), 4 (alarm).
- Produces: `writers.repos_with_recent_ops_event(db, component, event, *, days) -> set[str]` — Task 4's 7-day dedup lookback.
- Produces: `reva.config.DISMISSAL_RATE_ALERT: float` — read by Tasks 2, 3, 4.

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_precision_stats.py`:

```python
"""Per-repo review-precision aggregates (spec 2026-07-11).

Metric math on seeded feedback rows: posted-only denominator, explicit-/dismiss
and resolved_by_fix numerators, 30-day window, min-sample floor, threshold
flag, and the ops-event dedup lookback used by the scheduler alarm.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import OpsEvent, ReviewFinding, ReviewRun
from reva.types import Finding, JobParams, ReviewResult


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _finding(i: int) -> Finding:
    return Finding(severity="minor", category="style", file="x.py",
                   line_start=i + 1, line_end=i + 1, title=f"t{i}", body="b",
                   confidence=0.8, is_odoo_specific=False)


def _seed_repo(db, *, repo_num: int, name: str, total: int, posted: int) -> list[int]:
    """One completed review with `total` findings; the first `posted` of them
    marked as posted inline (comment-id backfill). Returns the posted ids."""
    repo_id = writers.upsert_repository(
        db, github_repository_id=repo_num, owner="acme", name=name,
        default_branch="main", installation_id=99,
    )
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=9000 + repo_num, pr_number=repo_num,
        title="PR", author_login="alice", base_branch="main", head_branch="feat",
        head_sha=f"sha{repo_num}", state="open", draft=False,
    )
    params = JobParams(
        repository_id=repo_id, pull_request_id=pr_id, head_sha=f"sha{repo_num}",
        installation_id=99, review_mode="diff", trigger_event="opened",
    )
    writers.record_review_completed(
        db, params,
        ReviewResult(status="completed", summary="ok", risk_level="low",
                     findings=[_finding(i) for i in range(total)]),
    )
    with db.session() as s:
        ids = [
            r.id for r in s.query(ReviewFinding)
            .join(ReviewRun, ReviewFinding.review_run_id == ReviewRun.id)
            .filter(ReviewRun.repository_id == repo_id)
            .order_by(ReviewFinding.id)
        ]
    posted_ids = ids[:posted]
    if posted_ids:
        writers.attach_finding_comment_ids(db, {fid: 100000 + fid for fid in posted_ids})
    return posted_ids


def _dismiss(db, finding_id: int, *, login: str = "alice") -> None:
    with db.session() as s:
        run_id = s.get(ReviewFinding, finding_id).review_run_id
    writers.record_feedback(
        db, review_finding_id=finding_id, review_run_id=run_id,
        github_comment_id=100000 + finding_id, reactor_login=login,
        reaction="dismissed", is_positive=False,
    )


def test_rates_over_posted_findings_only(db):
    posted = _seed_repo(db, repo_num=1, name="widgets", total=15, posted=12)
    for fid in posted[:5]:
        _dismiss(db, fid)
    for fid in posted[5:9]:
        writers.set_finding_outcome(db, fid, "resolved_by_fix")

    rows = writers.get_repo_precision_stats(db)

    assert rows == [{
        "repo": "acme/widgets",
        "findings_posted": 12,   # the 3 never-posted findings don't count
        "dismissed": 5,
        "fixed": 4,
        "dismissal_rate": round(5 / 12, 4),
        "addressed_rate": round(4 / 12, 4),
        "flagged": True,         # 12 >= floor 10 and 0.4167 > 0.30
    }]


def test_double_dismissal_counts_one_finding(db):
    posted = _seed_repo(db, repo_num=2, name="double", total=10, posted=10)
    _dismiss(db, posted[0], login="alice")
    _dismiss(db, posted[0], login="bob")
    assert writers.get_repo_precision_stats(db)[0]["dismissed"] == 1


def test_below_min_sample_floor_not_flagged(db):
    posted = _seed_repo(db, repo_num=3, name="small", total=4, posted=4)
    for fid in posted[:3]:
        _dismiss(db, fid)
    row = writers.get_repo_precision_stats(db)[0]
    assert row["dismissal_rate"] == 0.75
    assert row["flagged"] is False   # below the min-sample floor


def test_zero_posted_repo_has_no_row(db):
    # Findings persisted but never posted inline: no row, no division by zero.
    _seed_repo(db, repo_num=4, name="unposted", total=3, posted=0)
    assert writers.get_repo_precision_stats(db) == []


def test_window_excludes_old_findings(db):
    posted = _seed_repo(db, repo_num=5, name="old", total=10, posted=10)
    with db.session() as s:
        for fid in posted[:6]:
            s.get(ReviewFinding, fid).created_at = (
                datetime.now(timezone.utc) - timedelta(days=40)
            )
    assert writers.get_repo_precision_stats(db)[0]["findings_posted"] == 4


def test_threshold_zero_disables_flagging(db):
    posted = _seed_repo(db, repo_num=6, name="hot", total=12, posted=12)
    for fid in posted[:8]:
        _dismiss(db, fid)
    row = writers.get_repo_precision_stats(db, dismissal_alert=0.0)[0]
    assert row["flagged"] is False


def test_recent_ops_event_repos_lookback(db):
    writers.record_ops_event(db, "feedback", "warning", "dismissal_rate_high",
                             {"repo": "acme/widgets"})
    writers.record_ops_event(db, "feedback", "warning", "dismissal_rate_high",
                             {"repo": "acme/old"})
    writers.record_ops_event(db, "feedback", "warning", "other_event",
                             {"repo": "acme/other"})
    with db.session() as s:
        rows = s.query(OpsEvent).filter(OpsEvent.event == "dismissal_rate_high").all()
        old = next(r for r in rows if r.detail["repo"] == "acme/old")
        old.created_at = datetime.now(timezone.utc) - timedelta(days=8)

    assert writers.repos_with_recent_ops_event(
        db, "feedback", "dismissal_rate_high", days=7
    ) == {"acme/widgets"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_precision_stats.py -v`
Expected: FAIL — `AttributeError: module 'reva.db.writers' has no attribute 'get_repo_precision_stats'`.

- [ ] **Step 3: Implement**

`reva/config.py`, after `VERIFY_MODEL` (line 21):

```python
# Review-precision dismissal-rate alarm threshold (spec 2026-07-11). A repo with
# >= 10 posted inline findings in the trailing 30 days whose dismissal rate
# exceeds this is flagged (API/TUI/weekly report) and chat-alerted daily by the
# scheduler; 0 disables the flagging and the alarm. Lives here (not scheduler
# settings) because api, worker, and scheduler all read it.
DISMISSAL_RATE_ALERT = float(os.environ.get("REVA_DISMISSAL_RATE_ALERT", "0.30"))
```

`reva/db/writers.py`, after `get_memory_distill_input` (before the `# --- ticket_analyses writers` header, ~line 1211) — all names used (`case`, `func`, `select`, `Repository`, `ReviewFeedback`, `ReviewFinding`, `ReviewRun`, `datetime`, `timedelta`, `timezone`) are already imported at the top of the file:

```python
# --- review precision (spec 2026-07-11) ---------------------------------------


def get_repo_precision_stats(
    db: Database,
    *,
    window_days: int = 30,
    min_findings: int = 10,
    dismissal_alert: float = 0.30,
) -> list[dict]:
    """Per-repo review-precision aggregates over a trailing window, computed on
    read (no table — spec 2026-07-11). One row per repo with >= 1 inline
    finding actually posted in the window (posted_to_github, i.e. post-cap and
    post-mute):

      findings_posted  distinct posted findings
      dismissed        distinct posted findings with an explicit /dismiss
                       (review_feedback.reaction == 'dismissed'; a thumbs-down
                       reaction is NOT a dismissal)
      fixed            distinct posted findings with outcome 'resolved_by_fix'
      dismissal_rate   dismissed / findings_posted
      addressed_rate   fixed / findings_posted
      flagged          findings_posted >= min_findings AND dismissal_alert > 0
                       AND dismissal_rate > dismissal_alert

    Findings neither dismissed nor fixed are unaddressed context, not a third
    rate. Repos with no posted findings in the window simply have no row.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    with db.session() as s:
        rows = s.execute(
            select(
                Repository.full_name.label("repo"),
                func.count(func.distinct(ReviewFinding.id)).label("findings_posted"),
                func.count(func.distinct(
                    case((ReviewFeedback.reaction == "dismissed",
                          ReviewFeedback.review_finding_id))
                )).label("dismissed"),
                func.count(func.distinct(
                    case((ReviewFinding.outcome == "resolved_by_fix", ReviewFinding.id))
                )).label("fixed"),
            )
            .select_from(ReviewFinding)
            .join(ReviewRun, ReviewFinding.review_run_id == ReviewRun.id)
            .join(Repository, ReviewRun.repository_id == Repository.id)
            .outerjoin(ReviewFeedback, ReviewFeedback.review_finding_id == ReviewFinding.id)
            .where(ReviewFinding.posted_to_github.is_(True))
            .where(ReviewFinding.created_at >= cutoff)
            .group_by(Repository.full_name)
            .order_by(Repository.full_name)
        ).all()
    result = []
    for r in rows:
        # A row exists only with >= 1 posted finding, so the division is safe.
        dismissal_rate = round(r.dismissed / r.findings_posted, 4)
        addressed_rate = round(r.fixed / r.findings_posted, 4)
        result.append({
            "repo": r.repo,
            "findings_posted": r.findings_posted,
            "dismissed": r.dismissed,
            "fixed": r.fixed,
            "dismissal_rate": dismissal_rate,
            "addressed_rate": addressed_rate,
            "flagged": (
                dismissal_alert > 0
                and r.findings_posted >= min_findings
                and dismissal_rate > dismissal_alert
            ),
        })
    return result
```

(The `count(distinct case(...))` idiom mirrors `learning_stats` / `get_memory_distill_input` — it runs on both SQLite and Postgres.)

`reva/db/writers.py`, after `purge_old_ops_events` (~line 1408):

```python
def repos_with_recent_ops_event(
    db: Database, component: str, event: str, *, days: int
) -> set[str]:
    """Repos (detail['repo']) with a matching ops event in the trailing window.

    Persisted dedup lookback for recurring per-repo alarms — the dismissal-rate
    alarm fires at most once per repo per 7 days, surviving scheduler restarts.
    `detail` is JSON, so the repo key is matched in Python: dialect-safe, and
    the candidate set is tiny (alarm events only). Relies on
    REVA_OPS_EVENTS_RETENTION_DAYS (default 30) exceeding `days`.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with db.session() as s:
        details = s.execute(
            select(OpsEvent.detail)
            .where(OpsEvent.component == component)
            .where(OpsEvent.event == event)
            .where(OpsEvent.created_at >= cutoff)
        ).scalars().all()
    return {d["repo"] for d in details if isinstance(d, dict) and d.get("repo")}
```

`.env.example`, after the `# REVA_REPO_CACHE_DISK_PCT_ALERT=90` line (line 94):

```
# REVA_DISMISSAL_RATE_ALERT=0.30        # per-repo 30d dismissal-rate alarm threshold; 0 disables
```

`README.md`, in the env table after the `REVA_QUEUE_DEPTH_ALERT / REVA_FAILED_JOBS_ALERT / REVA_REPO_CACHE_DISK_PCT_ALERT` row (line 272):

```
| `REVA_DISMISSAL_RATE_ALERT` | — | `0.30` | Daily per-repo dismissal-rate alarm: fires when a repo with ≥10 posted findings in the trailing 30 days has more than this fraction `/dismiss`ed (needs `GOOGLE_CHAT_WEBHOOK_URL`; one alert per repo per 7 days; `0` disables) |
```

- [ ] **Step 4: Run the covering suites**

Run: `cd worker && .venv/bin/python -m pytest tests/test_precision_stats.py tests/test_env_example.py tests/test_db.py tests/test_ops_events.py -q`
Expected: all PASS (env-example drift test proves the `.env.example` row; existing writers suites prove no regression).

- [ ] **Step 5: Commit**

```bash
git add reva/db/writers.py reva/config.py .env.example README.md worker/tests/test_precision_stats.py
git commit -m "feat(metrics): per-repo 30d review-precision aggregates + dismissal-alert threshold"
```

---

### Task 2: API — GET /api/v1/metrics/precision

**Files:**
- Modify: `api/app/queries/metrics.py` (add `precision_stats` at end of file, after `learned_memory` ~line 405)
- Modify: `api/app/schemas/metrics.py` (add `PrecisionStat` after `LearningStat`, ~line 73)
- Modify: `api/app/routes/v1/metrics.py` (import + route after `/metrics/learning`, ~line 57)
- Modify: `api/tests/test_v1_metrics.py` (new section at end, ~line 317)

**Interfaces:**
- Consumes: `writers.get_repo_precision_stats` and `reva.config.DISMISSAL_RATE_ALERT` (Task 1).
- Produces: `GET /api/v1/metrics/precision -> list[PrecisionStat]` with fields `repo: str, findings_posted: int, dismissed: int, fixed: int, dismissal_rate: float, addressed_rate: float, flagged: bool` — Task 5's Go client unmarshals it.

- [ ] **Step 1: Write the failing tests**

In `api/tests/test_v1_metrics.py`, append (reuses the file's `_seed_review_with_findings` helper at line 229):

```python
# --- precision (review-precision metrics, spec 2026-07-11) --------------------


def test_precision_empty(client_and_db):
    client, _ = client_and_db
    resp = client.get("/api/v1/metrics/precision")
    assert resp.status_code == 200 and resp.json() == []


def test_precision_per_repo_rates_and_flag(client_and_db):
    from reva.db.models import ReviewFinding
    from reva.types import Finding

    client, db = client_and_db
    findings = [
        Finding(severity="minor", category="style", file="x.py", line_start=i + 1,
                line_end=i + 1, title=f"t{i}", body="b", confidence=0.8,
                is_odoo_specific=False)
        for i in range(10)
    ]
    _seed_review_with_findings(db, findings)
    with db.session() as s:
        ids = [(r.id, r.review_run_id)
               for r in s.query(ReviewFinding).order_by(ReviewFinding.id)]
    writers.attach_finding_comment_ids(db, {fid: 100 + fid for fid, _ in ids})
    for fid, rr in ids[:4]:
        writers.record_feedback(db, review_finding_id=fid, review_run_id=rr,
                                github_comment_id=100 + fid, reactor_login="alice",
                                reaction="dismissed", is_positive=False)
    writers.set_finding_outcome(db, ids[4][0], "resolved_by_fix")

    data = client.get("/api/v1/metrics/precision").json()
    assert data == [{
        "repo": "acme/widgets",
        "findings_posted": 10,
        "dismissed": 4,
        "fixed": 1,
        "dismissal_rate": 0.4,
        "addressed_rate": 0.1,
        "flagged": True,  # 10 >= floor and 0.4 > default REVA_DISMISSAL_RATE_ALERT 0.30
    }]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_metrics.py -k precision -v`
Expected: FAIL — 404 (`assert resp.status_code == 200` fails; route doesn't exist yet).

- [ ] **Step 3: Implement**

`api/app/schemas/metrics.py`, after `LearningStat`:

```python
class PrecisionStat(BaseModel):
    repo: str
    findings_posted: int
    dismissed: int
    fixed: int
    dismissal_rate: float
    addressed_rate: float
    flagged: bool
```

`api/app/queries/metrics.py` — add `from reva import config` to the imports, and at end of file:

```python
def precision_stats(db: Database) -> list[dict]:
    """Per-repo rolling-30d review-precision aggregates (spec 2026-07-11).

    Computed on read — no table; flagging uses the shared alarm threshold so
    the TUI warning, the weekly report flag, and the scheduler alarm agree.
    """
    return writers.get_repo_precision_stats(
        db, dismissal_alert=config.DISMISSAL_RATE_ALERT
    )
```

`api/app/routes/v1/metrics.py` — add `PrecisionStat` to the `app.schemas.metrics` import block, and after the `/metrics/learning` route:

```python
@router.get("/metrics/precision", response_model=list[PrecisionStat])
def precision(db: Database = Depends(get_db)) -> list[dict]:
    return [PrecisionStat.model_validate(r) for r in q.precision_stats(db)]
```

- [ ] **Step 4: Run the API suite**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_metrics.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add api/app/queries/metrics.py api/app/schemas/metrics.py api/app/routes/v1/metrics.py api/tests/test_v1_metrics.py
git commit -m "feat(api): /metrics/precision — per-repo dismissal/addressed rates"
```

---

### Task 3: Weekly report — per-repo precision section

**Files:**
- Modify: `reva/weekly_report.py` (stats dict ~line 179; formatter after the "By repository" block ~line 272; imports)
- Modify: `worker/tests/test_weekly_report.py`

**Interfaces:**
- Consumes: `writers.get_repo_precision_stats` + `config.DISMISSAL_RATE_ALERT` (Task 1).
- Produces: `weekly_report_stats(...)["precision"]: list[dict]` and a `*Review precision (last 30 days)*` section in the Chat message.

- [ ] **Step 1: Write the failing test**

In `worker/tests/test_weekly_report.py`, append (imports at top of the new helper keep the file's existing header untouched):

```python
def _seed_precision_repo(db: Database) -> None:
    """12 posted findings, 5 dismissed, 4 fixed -> 42% dismissed, flagged."""
    from reva.db.models import ReviewFinding
    from reva.types import Finding, JobParams, ReviewResult

    repo_id = writers.upsert_repository(
        db, github_repository_id=7007, owner="acme", name="widgets",
        default_branch="main", installation_id=99,
    )
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=8008, pr_number=8, title="PR",
        author_login="alice", base_branch="main", head_branch="feat",
        head_sha="s8", state="open", draft=False,
    )
    params = JobParams(repository_id=repo_id, pull_request_id=pr_id, head_sha="s8",
                       installation_id=99, review_mode="diff", trigger_event="opened")
    findings = [
        Finding(severity="minor", category="style", file="x.py", line_start=i + 1,
                line_end=i + 1, title=f"t{i}", body="b", confidence=0.8,
                is_odoo_specific=False)
        for i in range(12)
    ]
    writers.record_review_completed(
        db, params,
        ReviewResult(status="completed", summary="ok", risk_level="low",
                     findings=findings),
    )
    with db.session() as s:
        pairs = [(r.id, r.review_run_id)
                 for r in s.query(ReviewFinding).order_by(ReviewFinding.id)]
    writers.attach_finding_comment_ids(db, {fid: 100 + fid for fid, _ in pairs})
    for fid, rr in pairs[:5]:
        writers.record_feedback(db, review_finding_id=fid, review_run_id=rr,
                                github_comment_id=100 + fid, reactor_login="alice",
                                reaction="dismissed", is_positive=False)
    for fid, _ in pairs[5:9]:
        writers.set_finding_outcome(db, fid, "resolved_by_fix")


def test_weekly_report_includes_precision_section(db: Database) -> None:
    _seed_precision_repo(db)
    report = build_weekly_report(db)
    assert "*Review precision (last 30 days)*" in report
    assert "⚠ `acme/widgets`: 12 posted · 42% dismissed · 33% fixed" in report
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd worker && .venv/bin/python -m pytest tests/test_weekly_report.py -k precision -v`
Expected: FAIL — `KeyError: 'precision'` (or missing section string).

- [ ] **Step 3: Implement**

`reva/weekly_report.py` — add to the imports:

```python
from reva import config
```

In the `weekly_report_stats` return dict, after `"ready_tickets": ...` (line 179 — note this sits outside the `with db.session()` block, like `list_ready_tickets` it opens its own session):

```python
        "precision": writers.get_repo_precision_stats(
            db, dismissal_alert=config.DISMISSAL_RATE_ALERT
        ),
```

In `build_weekly_report`, after the "By repository" block (after line 272's `lines.append("")`):

```python
    # --- Review precision (30-day rolling window, spec 2026-07-11) ---
    if stats["precision"]:
        lines.append("*Review precision (last 30 days)*")
        for p in stats["precision"]:
            flag = "⚠ " if p["flagged"] else ""
            lines.append(
                f"  {flag}`{p['repo']}`: {p['findings_posted']} posted · "
                f"{p['dismissal_rate']:.0%} dismissed · {p['addressed_rate']:.0%} fixed"
            )
        lines.append("")
```

(The precision window is the metric's own 30-day roll, independent of the report's 7-day `since` — that's the spec's trend view, and the section title says so.)

- [ ] **Step 4: Run the covering suites**

Run: `cd worker && .venv/bin/python -m pytest tests/test_weekly_report.py tests/test_precision_stats.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add reva/weekly_report.py worker/tests/test_weekly_report.py
git commit -m "feat(report): per-repo review-precision section in the weekly report"
```

---

### Task 4: Scheduler — daily dismissal-rate alarm with persisted 7-day dedup

**Files:**
- Modify: `scheduler/scheduler/main.py` (new `maybe_check_dismissal_rates` after `maybe_purge_ticket_text` ~line 127; loop wiring after the value-report block ~line 241; `last_dismissal_check = None` init ~line 180; imports)
- Create: `scheduler/tests/test_dismissal_alarm.py`

**Interfaces:**
- Consumes: `writers.get_repo_precision_stats`, `writers.repos_with_recent_ops_event`, `writers.record_ops_event` (Task 1 + existing), `reva.notifications.notify_operational_alert(webhook_url, title, detail) -> bool`, `reva.config.DISMISSAL_RATE_ALERT`.
- Produces: `maybe_check_dismissal_rates(db, now, last_check, threshold, webhook_url, interval_s=86_400) -> datetime | None` — same `maybe_*` cadence contract as `maybe_enqueue_eviction` (returns the new last-check timestamp, unchanged if not due).

- [ ] **Step 1: Write the failing tests**

Create `scheduler/tests/test_dismissal_alarm.py`:

```python
"""Daily dismissal-rate alarm (review-precision spec 2026-07-11).

The metric math is covered in worker/tests/test_precision_stats.py; here the
precision query is faked and the alarm logic — floor/threshold gating, chat +
ops event, persisted 7-day dedup, delivered-gating — runs against a real
SQLite ops_events table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from reva.db import Base, Database, create_engine_from_url
from reva.db.models import OpsEvent
from scheduler.main import maybe_check_dismissal_rates


@pytest.fixture()
def db():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _now() -> datetime:
    return datetime(2026, 7, 12, 6, 0, 0, tzinfo=timezone.utc)


_FLAGGED = [{"repo": "acme/widgets", "findings_posted": 12, "dismissed": 5,
             "fixed": 4, "dismissal_rate": 0.4167, "addressed_rate": 0.3333,
             "flagged": True}]
_BELOW_FLOOR = [{"repo": "acme/small", "findings_posted": 4, "dismissed": 3,
                 "fixed": 0, "dismissal_rate": 0.75, "addressed_rate": 0.0,
                 "flagged": False}]


def _check(db, *, rows, last=None, threshold=0.30,
           webhook="https://chat.example/hook", delivered=True, now=None):
    with patch("scheduler.main.writers.get_repo_precision_stats",
               return_value=rows), \
         patch("scheduler.main.notify_operational_alert",
               return_value=delivered) as notify:
        new_last = maybe_check_dismissal_rates(
            db, now or _now(), last, threshold, webhook)
    return new_last, notify


def test_alarm_fires_and_records_ops_event(db):
    new_last, notify = _check(db, rows=_FLAGGED)
    assert new_last == _now()
    assert notify.call_count == 1
    assert "acme/widgets" in notify.call_args[0][2]
    with db.session() as s:
        ev = s.query(OpsEvent).one()
    assert (ev.component, ev.severity, ev.event) == (
        "feedback", "warning", "dismissal_rate_high")
    assert ev.detail["repo"] == "acme/widgets"


def test_alarm_silent_when_below_floor_or_threshold(db):
    _, notify = _check(db, rows=_BELOW_FLOOR)   # flagged=False rows never alert
    notify.assert_not_called()
    with db.session() as s:
        assert s.query(OpsEvent).count() == 0


def test_alarm_dedups_per_repo_for_seven_days(db):
    _check(db, rows=_FLAGGED)                                   # day 1: fires
    _, notify = _check(db, rows=_FLAGGED, now=_now() + timedelta(days=1))
    notify.assert_not_called()                                  # day 2: deduped
    with db.session() as s:
        assert s.query(OpsEvent).count() == 1


def test_alarm_refires_after_dedup_window(db):
    _check(db, rows=_FLAGGED)
    with db.session() as s:
        s.query(OpsEvent).one().created_at = (
            datetime.now(timezone.utc) - timedelta(days=8))
    _, notify = _check(db, rows=_FLAGGED)
    assert notify.call_count == 1


def test_alarm_disabled_at_zero_threshold(db):
    new_last, notify = _check(db, rows=_FLAGGED, threshold=0.0)
    assert new_last is None          # check skipped entirely, timer untouched
    notify.assert_not_called()


def test_alarm_gated_to_daily_interval(db):
    last = _now() - timedelta(hours=2)
    new_last, notify = _check(db, rows=_FLAGGED, last=last)
    assert new_last == last
    notify.assert_not_called()


def test_failed_send_leaves_no_marker_so_next_run_retries(db):
    _check(db, rows=_FLAGGED, delivered=False)
    with db.session() as s:
        assert s.query(OpsEvent).count() == 0   # no dedup marker
    _, notify = _check(db, rows=_FLAGGED)       # next daily run re-attempts
    assert notify.call_count == 1


def test_empty_webhook_still_records_ops_event(db):
    # Chat disabled: the breach is still visible in ops events / Failures tab.
    _, notify = _check(db, rows=_FLAGGED, webhook="", delivered=False)
    with db.session() as s:
        assert s.query(OpsEvent).one().event == "dismissal_rate_high"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd scheduler && .venv/bin/python -m pytest tests/test_dismissal_alarm.py -v`
Expected: FAIL — `ImportError: cannot import name 'maybe_check_dismissal_rates' from 'scheduler.main'`.

- [ ] **Step 3: Implement**

`scheduler/scheduler/main.py` — extend the imports:

```python
from reva import config
from reva.notifications import notify_operational_alert
```

Module constant near the top (after `logger = structlog.get_logger()`):

```python
# Dismissal-rate alarm cadence — daily, like eviction/purge. Not env-tunable;
# only the threshold (REVA_DISMISSAL_RATE_ALERT) is.
_DISMISSAL_ALARM_INTERVAL_S = 86_400
```

New function after `maybe_purge_ticket_text` (~line 127):

```python
def maybe_check_dismissal_rates(
    db, now, last_check, threshold, webhook_url,
    interval_s=_DISMISSAL_ALARM_INTERVAL_S,
):
    """Daily dismissal-rate alarm (review-precision spec 2026-07-11).

    Any repo whose trailing-30-day dismissal_rate crossed `threshold` with the
    min-sample floor met (rows come back `flagged` from the shared metric) gets
    one Google Chat alert + one 'dismissal_rate_high' ops event — at most once
    per repo per 7 days. The ops event IS the persisted dedup marker, so a
    scheduler restart (last_check resets to None) cannot re-page. `threshold`
    <= 0 disables the check. Returns the new last-check timestamp (unchanged
    if not yet due).
    """
    if threshold <= 0:
        return last_check
    if last_check is not None and (now - last_check).total_seconds() < interval_s:
        return last_check
    rows = writers.get_repo_precision_stats(db, dismissal_alert=threshold)
    already = writers.repos_with_recent_ops_event(
        db, "feedback", "dismissal_rate_high", days=7
    )
    for r in rows:
        if not r["flagged"] or r["repo"] in already:
            continue
        delivered = notify_operational_alert(
            webhook_url,
            "High dismissal rate",
            f"`{r['repo']}`: {r['dismissal_rate']:.0%} of {r['findings_posted']} "
            f"findings posted in the last 30 days were dismissed "
            f"(threshold {threshold:.0%}). REVA may be noise on this repo — "
            "consider tuning .claude-review.yml or /mute-ing categories.",
        )
        if delivered or not webhook_url:
            # The ops event doubles as the 7-day dedup marker: record it only
            # when the alert was delivered (or Chat is off entirely), so a
            # transient Chat failure re-attempts tomorrow instead of being
            # swallowed for a week (mirrors Monitor's delivered-gating).
            writers.record_ops_event(
                db, "feedback", "warning", "dismissal_rate_high",
                {"repo": r["repo"], "dismissal_rate": r["dismissal_rate"],
                 "findings_posted": r["findings_posted"], "threshold": threshold},
            )
        logger.warning("dismissal_rate_high", repo=r["repo"],
                       rate=r["dismissal_rate"], posted=r["findings_posted"],
                       delivered=delivered)
    return now
```

In `main()`, with the other timer inits (~line 180):

```python
    # Dismissal-rate alarm: run one shortly after startup, then daily; the
    # ops-event dedup makes the startup run safe.
    last_dismissal_check = None
```

In the loop, after the value-report try/except (~line 241):

```python
        try:
            last_dismissal_check = maybe_check_dismissal_rates(
                db, now, last_dismissal_check,
                config.DISMISSAL_RATE_ALERT,
                settings.google_chat_webhook_url,
            )
        except Exception:
            logger.exception("scheduler_dismissal_alarm_error")
```

- [ ] **Step 4: Run the scheduler suite**

Run: `cd scheduler && .venv/bin/python -m pytest tests/ -q`
Expected: all PASS (including the untouched monitor/reporter/poller suites).

- [ ] **Step 5: Commit**

```bash
git add scheduler/scheduler/main.py scheduler/tests/test_dismissal_alarm.py
git commit -m "feat(scheduler): daily dismissal-rate alarm with 7-day ops-event dedup"
```

---

### Task 5: TUI — per-repo precision on the Feedback tab

**Files:**
- Modify: `tui/internal/api/types.go` (add `PrecisionStat` after `LearningStat`, ~line 347)
- Modify: `tui/internal/api/iface.go` (add `Precision()` after `LearnedMemory()`, line 25)
- Modify: `tui/internal/api/client.go` (add `Precision()` after `LearnedMemory()`, ~line 220)
- Modify: `tui/internal/api/mock.go` (add `Precision()` after `LearnedMemory()`, ~line 698 — demo-mode coverage, house rule)
- Modify: `tui/internal/ui/messages.go` (`feedbackLoadedMsg`, line 81)
- Modify: `tui/internal/ui/feedback.go` (struct line 16, `load()` line 32, `update()` line 52, `bodyLines()` line 94, doc comment line 12)
- Modify: `tui/internal/ui/feedback_test.go`
- Modify: `tui/README.md` (Feedback-tab description, ~line 47)

**Interfaces:**
- Consumes: Task 2's JSON (`GET /metrics/precision`).
- Produces: `Precision() ([]PrecisionStat, error)` on `api.ClientIface`, implemented by both `Client` and `MockClient`.

- [ ] **Step 1: Write the failing Go test**

In `tui/internal/ui/feedback_test.go`, append:

```go
func TestFeedbackShowsPrecision(t *testing.T) {
	f := newFeedback(&api.MockClient{})
	f, _ = f.update(f.load()())
	if len(f.precision) == 0 {
		t.Fatal("expected mock precision rows")
	}
	out := f.view(120, 40)
	for _, want := range []string{"Posted", "Dism%", "Fixed%", "acme/odoo-modules", "30%"} {
		if !strings.Contains(out, want) {
			t.Errorf("view missing %q:\n%s", want, out)
		}
	}
}
```

Run: `cd tui && go test ./internal/ui/ -run TestFeedbackShowsPrecision`
Expected: FAIL to compile — `f.precision undefined`.

- [ ] **Step 2: API types + client + mock**

`tui/internal/api/types.go`, after `LearningStat` (~line 347):

```go
// PrecisionStat is one repo's rolling-30-day review-precision aggregate:
// inline findings actually posted vs. explicit /dismiss and verified fixes.
// Flagged repos crossed the dismissal-rate alarm threshold with the
// min-sample floor met. Served by GET /api/v1/metrics/precision.
type PrecisionStat struct {
	Repo           string  `json:"repo"`
	FindingsPosted int     `json:"findings_posted"`
	Dismissed      int     `json:"dismissed"`
	Fixed          int     `json:"fixed"`
	DismissalRate  float64 `json:"dismissal_rate"`
	AddressedRate  float64 `json:"addressed_rate"`
	Flagged        bool    `json:"flagged"`
}
```

`tui/internal/api/iface.go`, after `LearnedMemory()` (line 25):

```go
	Precision() ([]PrecisionStat, error)
```

`tui/internal/api/client.go`, after `LearnedMemory()` (~line 220):

```go
func (c *Client) Precision() ([]PrecisionStat, error) {
	var out []PrecisionStat
	return out, c.get("/metrics/precision", &out)
}
```

`tui/internal/api/mock.go`, after `LearnedMemory()` (~line 698) — one flagged repo, one healthy, one below-floor with a high rate (shows the floor working in demo mode):

```go
func (m *MockClient) Precision() ([]PrecisionStat, error) {
	return []PrecisionStat{
		{Repo: "acme/odoo-modules", FindingsPosted: 46, Dismissed: 14, Fixed: 21,
			DismissalRate: 0.3043, AddressedRate: 0.4565, Flagged: true},
		{Repo: "acme/website", FindingsPosted: 12, Dismissed: 1, Fixed: 8,
			DismissalRate: 0.0833, AddressedRate: 0.6667, Flagged: false},
		{Repo: "acme/internal-tools", FindingsPosted: 4, Dismissed: 3, Fixed: 0,
			DismissalRate: 0.75, AddressedRate: 0, Flagged: false},
	}, nil
}
```

- [ ] **Step 3: Wire the Feedback tab**

`tui/internal/ui/messages.go`, replace `feedbackLoadedMsg` (line 81):

```go
type feedbackLoadedMsg struct {
	stats     []api.LearningStat
	mutes     []api.MuteEntry
	memory    []api.LearnedMemoryEntry
	precision []api.PrecisionStat
	err       error
}
```

`tui/internal/ui/feedback.go`:
- Struct (line 16): add `precision []api.PrecisionStat` after `memory`.
- Doc comment (lines 12-15): extend with "…plus per-repo 30-day precision rates (dismissed%/fixed%; flagged repos exceed the dismissal-rate alarm threshold)."
- `load()` (line 32): before the `memory, err := client.LearnedMemory()` line, add:

```go
		precision, err := client.Precision()
		if err != nil {
			return feedbackLoadedMsg{err: err}
		}
```

and change the final return to:

```go
		memory, err := client.LearnedMemory()
		return feedbackLoadedMsg{stats: stats, mutes: mutes, memory: memory,
			precision: precision, err: err}
```

- `update()` (case `feedbackLoadedMsg`, line 52): add `f.precision = m.precision` next to the other copies.
- `bodyLines()` (line 94): hoist the column widths and insert the precision table before the per-category stats table. Replace the function's opening:

```go
func (f Feedback) bodyLines() []string {
	var body []string
	body = append(body, "")
	colRepo, colCat := 30, 16

	if len(f.precision) > 0 {
		body = append(body, lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
			fmt.Sprintf("  %-*s  %8s  %7s  %7s",
				colRepo, "Repository (30d)", "Posted", "Dism%", "Fixed%")))
		for _, p := range f.precision {
			line := fmt.Sprintf("  %-*s  %8d  %6.0f%%  %6.0f%%",
				colRepo, truncate(p.Repo, colRepo), p.FindingsPosted,
				p.DismissalRate*100, p.AddressedRate*100)
			if p.Flagged {
				// Same warning treatment the TUI uses for stale runs.
				line = styleStatusStale.Render(line)
			}
			body = append(body, line)
		}
		body = append(body, "")
	}

	if len(f.stats) == 0 {
```

and in the `else` branch below, drop the now-duplicate `colRepo, colCat := 30, 16` declaration (the header/rows keep using the hoisted variables).

`tui/README.md` (~line 47): extend the Feedback-tab sentence to mention the per-repo 30-day precision table from `GET /metrics/precision`.

- [ ] **Step 4: Gate**

Run: `cd tui && go build ./... && go vet ./... && go test ./... && gofmt -l .`
Expected: build/vet/test green (including `TestFeedbackShowsPrecision` and the pre-existing feedback tests); `gofmt -l` prints nothing.

Optional visual check: `cd tui && go run . --demo` → Feedback tab shows the precision table with `acme/odoo-modules` in the yellow warning style.

- [ ] **Step 5: Commit**

```bash
git add tui/internal/api/types.go tui/internal/api/iface.go tui/internal/api/client.go tui/internal/api/mock.go tui/internal/ui/messages.go tui/internal/ui/feedback.go tui/internal/ui/feedback_test.go tui/README.md
git commit -m "feat(tui): per-repo precision rates on the Feedback tab"
```

---

### Task 6: Verification sweep + spec sync

**Files:**
- Modify: `docs/superpowers/specs/2026-07-11-review-precision-metrics-design.md` (Status line + field-mapping corrections)

- [ ] **Step 1: Full gates** (shared `reva/` touched → all three services)

Run: `make test` → worker/api/scheduler all green.
Run: `worker/.venv/bin/ruff check reva worker/worker api/app scheduler/scheduler` → clean.
Run: `worker/.venv/bin/mypy reva worker/worker api/app scheduler/scheduler --ignore-missing-imports` → no NEW errors vs. main (advisory).
Run: `cd tui && go build ./... && go vet ./... && go test ./... && gofmt -l .` → green, no gofmt output.

- [ ] **Step 2: Spec sync**

In the spec, replace the Status line with:

```
- **Status:** approved — implemented; see plans/2026-07-11-review-precision-metrics.md
```

and append a short "Planning notes (2026-07-11)" block recording: (a) ops-event `kind` maps to the `ops_events.event` column (`component="feedback"`, `severity="warning"`, `event="dismissal_rate_high"`, `detail.repo`); (b) the per-repo rates render as a summary table above the per-(repo, category) table (the existing table's grain can't host repo-level columns); (c) the 7-day dedup marker is the recorded ops event itself (the weekly report's dedup table approach would need a new table, which the spec forbids) — code wins over prose.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-07-11-review-precision-metrics-design.md
git commit -m "docs(specs): review precision metrics — mark implemented, record field mapping"
```

**Honest-status note for the final report:** all coverage is unit-level (SQLite in-memory, mocked Chat delivery); the `count(distinct case(...))` idiom is the same one `learning_stats` already runs on production Postgres. The live pieces that only staging can prove: the real Google Chat send, the daily cadence in the running scheduler loop, and the Postgres query plan on real data volumes. `findings_posted` inherits the comment-id backfill's best-effort nature (`_backfill_comment_ids`): findings whose inline comment never matched are not counted — the same source of truth `/dismiss` uses, so numerator and denominator stay consistent.
