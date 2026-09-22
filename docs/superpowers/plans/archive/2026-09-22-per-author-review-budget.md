# Per-author review budget — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cap PR-review spend per PR author (default $100 / rolling 24 h) and narrow the global cap `REVA_DAILY_BUDGET_USD` to non-review Claude spend.

**Architecture:** A new writer sums `review_runs.estimated_cost_usd` per `pull_requests.author_login` over the trailing 24 h; a new `author_budget_exceeded` gate in the worker runner replaces the global gate on the three review-spend call sites (pre-flight decline, self-critique flag, delta finding resolution). The global gate keeps every other caller but excludes the ledger kinds `review` and `delta_verify`. No migration, no API/TUI change.

**Tech Stack:** Python 3.14, SQLAlchemy 2.0, pytest (SQLite in-memory), per-service venvs (`worker/.venv`).

**Spec:** `docs/superpowers/specs/archive/2026-09-22-per-author-review-budget-design.md`

## Global Constraints

- Env var name: `REVA_AUTHOR_DAILY_BUDGET_USD`; default `100`; a value `<= 0` disables the cap (`None`).
- Decline text (verbatim): `REVA's rolling 24-hour review budget for @<login> ($<cap>) has been reached (≈$<spent> spent). Reviews resume automatically as spend rolls off.` — `<cap>` and `<spent>` formatted with `:.0f`.
- Global gate excludes ledger kinds `("review", "delta_verify")`.
- Per-author sum counts paid runs (`estimated_cost_usd > 0`) regardless of status, because `review_runs` rows are upserted per (repo, pr, sha, mode) and a later decline/failure on the same SHA reuses the paid row, with `completed_at >= since`; a PR whose `author_login` is NULL is not gated.
- Comments and docs in English. No commits by the implementer — Joseph commits himself.
- Definition of done: `worker`, `api`, `scheduler` suites green (`make test`) + `ruff check reva worker/worker api/app scheduler/scheduler`.

Run tests from the repo root with the worker venv: `worker/.venv/bin/python -m pytest worker/tests/<file> -v` (the worker venv has `reva/` installed editable; `api/` and `scheduler/` need no change here but `make test` runs them too because `reva/db/writers.py` is shared).

---

### Task 1: Spend writers — `exclude_kinds` and the per-author sum

**Files:**
- Modify: `reva/db/writers.py:366-393` (`sum_estimated_cost_since`), add a new function right after it
- Test: `worker/tests/test_author_budget_writers.py` (new)

**Interfaces:**
- Produces: `sum_estimated_cost_since(db, since, *, serialize=False, exclude_kinds: tuple[str, ...] = ()) -> float`
- Produces: `sum_author_review_cost_since(db, pull_request_id: int, since: datetime, *, serialize=False) -> float | None`

- [ ] **Step 1: Write the failing tests**

```python
"""Per-author review spend writer and the global-ledger kind exclusion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import ReviewRun


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _repo(db: Database, name: str) -> int:
    return writers.upsert_repository(
        db, github_repository_id=hash(name) % 100000, owner="acme", name=name,
        default_branch="main", installation_id=500,
    )


def _pr(db: Database, repo_id: int, number: int, author: str | None) -> int:
    return writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=repo_id * 1000 + number,
        pr_number=number, title="t", author_login=author, base_branch="main",
        head_branch="f", head_sha=f"sha{number}", state="open", draft=False,
    )


def _run(db: Database, repo_id: int, pr_id: int, cost: float, *,
         status: str = "completed", hours_old: float = 0) -> None:
    with db.session() as s:
        s.add(ReviewRun(
            repository_id=repo_id, pull_request_id=pr_id, head_sha=f"h{pr_id}{cost}",
            status=status, trigger_event="opened", review_mode="diff",
            estimated_cost_usd=cost,
            completed_at=datetime.now(timezone.utc) - timedelta(hours=hours_old),
        ))


SINCE = datetime.now(timezone.utc) - timedelta(days=1)


def test_author_sum_spans_repos_for_same_login(db):
    r1, r2 = _repo(db, "one"), _repo(db, "two")
    p1, p2 = _pr(db, r1, 1, "alice"), _pr(db, r2, 7, "alice")
    _run(db, r1, p1, 3.0)
    _run(db, r2, p2, 4.5)
    assert writers.sum_author_review_cost_since(db, p1, SINCE) == pytest.approx(7.5)


def test_author_sum_ignores_other_authors(db):
    r = _repo(db, "one")
    pa, pb = _pr(db, r, 1, "alice"), _pr(db, r, 2, "bob")
    _run(db, r, pa, 3.0)
    _run(db, r, pb, 40.0)
    assert writers.sum_author_review_cost_since(db, pa, SINCE) == pytest.approx(3.0)


def test_author_sum_ignores_non_completed_and_old_runs(db):
    r = _repo(db, "one")
    p = _pr(db, r, 1, "alice")
    _run(db, r, p, 3.0)
    _run(db, r, p, 50.0, status="declined")
    _run(db, r, p, 60.0, status="failed")
    _run(db, r, p, 70.0, hours_old=25)
    assert writers.sum_author_review_cost_since(db, p, SINCE) == pytest.approx(3.0)


def test_author_sum_none_without_author(db):
    r = _repo(db, "one")
    p = _pr(db, r, 1, None)
    _run(db, r, p, 3.0)
    assert writers.sum_author_review_cost_since(db, p, SINCE) is None


def test_author_sum_zero_when_no_runs(db):
    r = _repo(db, "one")
    p = _pr(db, r, 1, "alice")
    assert writers.sum_author_review_cost_since(db, p, SINCE) == 0.0


def test_global_sum_excludes_kinds(db):
    writers.record_claude_spend(db, "review", 50.0)
    writers.record_claude_spend(db, "delta_verify", 0.5)
    writers.record_claude_spend(db, "audit", 2.0)
    assert writers.sum_estimated_cost_since(db, SINCE) == pytest.approx(52.5)
    assert writers.sum_estimated_cost_since(
        db, SINCE, exclude_kinds=("review", "delta_verify")
    ) == pytest.approx(2.0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `worker/.venv/bin/python -m pytest worker/tests/test_author_budget_writers.py -v`
Expected: the five author tests fail with `AttributeError: module 'reva.db.writers' has no attribute 'sum_author_review_cost_since'`; the global test fails with `TypeError: ... unexpected keyword argument 'exclude_kinds'`.

- [ ] **Step 3: Implement**

In `reva/db/writers.py`, change the signature and query of `sum_estimated_cost_since`:

```python
def sum_estimated_cost_since(
    db: Database,
    since: datetime,
    *,
    serialize: bool = False,
    exclude_kinds: tuple[str, ...] = (),
) -> float:
    """Total estimated USD cost of Claude calls recorded in the claude_spend
    ledger at/after `since`, minus the `exclude_kinds` (ledger `kind` values).

    Used by the worker's rolling spend cap — the ledger is the single accounting
    source. The global cap excludes review spend ("review", "delta_verify"),
    which is capped per PR author instead (sum_author_review_cost_since).

    With serialize=True the read is taken under a transaction-level advisory
    lock on Postgres, so concurrent workers evaluate the cap one at a time
    rather than racing on interleaved reads. (No-op on SQLite.) Residual
    overshoot is still bounded by the number of concurrent workers — at most one
    in-flight call each — which is accepted; the cap is a rolling guardrail.
    """
    from sqlalchemy import func as _func

    with db.session() as s:
        if serialize and s.get_bind().dialect.name == "postgresql":
            s.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": _BUDGET_ADVISORY_LOCK_KEY},
            )
        q = select(_func.coalesce(_func.sum(ClaudeSpend.cost_usd), 0.0)).where(
            ClaudeSpend.created_at >= since
        )
        if exclude_kinds:
            q = q.where(ClaudeSpend.kind.not_in(exclude_kinds))
        total = s.execute(q).scalar_one()
    return float(total or 0.0)
```

Add directly below it:

```python
def sum_author_review_cost_since(
    db: Database,
    pull_request_id: int,
    since: datetime,
    *,
    serialize: bool = False,
) -> float | None:
    """Rolling review spend (USD) of the author of `pull_request_id`, across all
    repos: completed review_runs with completed_at >= since, joined through
    pull_requests.author_login. None when the PR has no author login (the
    per-author cap does not apply). serialize=True takes the same advisory
    lock as sum_estimated_cost_since.
    """
    from sqlalchemy import func as _func

    with db.session() as s:
        if serialize and s.get_bind().dialect.name == "postgresql":
            s.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": _BUDGET_ADVISORY_LOCK_KEY},
            )
        author = s.execute(
            select(PullRequest.author_login).where(PullRequest.id == pull_request_id)
        ).scalar_one_or_none()
        if author is None:
            return None
        total = s.execute(
            select(_func.coalesce(_func.sum(ReviewRun.estimated_cost_usd), 0.0))
            .join(PullRequest, PullRequest.id == ReviewRun.pull_request_id)
            .where(
                PullRequest.author_login == author,
                ReviewRun.status == "completed",
                ReviewRun.completed_at >= since,
            )
        ).scalar_one()
    return float(total or 0.0)
```

`PullRequest` and `ReviewRun` are already imported at the top of `writers.py` (check with `grep -n "^from reva.db.models import" -A 30 reva/db/writers.py`; add them to that import list if missing).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `worker/.venv/bin/python -m pytest worker/tests/test_author_budget_writers.py worker/tests/test_spend_retention.py worker/tests/test_instance_quota_writers.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint**

Run: `ruff check reva`
Expected: no errors.

---

### Task 2: Config plumbing — settings, context, compose, docs

**Files:**
- Modify: `worker/worker/settings.py:35-38` (dataclass field) and `:84-88` (from_env)
- Modify: `worker/worker/runner.py:101` (`WorkerContext`), `:207` (`build_worker_context`)
- Modify: `docker-compose.prod.yml:188-189`, `docker-compose.yml:99-100` (worker `environment:`)
- Modify: `.env.example:49-52`, `README.md:337`
- Test: `worker/tests/test_settings.py`

**Interfaces:**
- Produces: `Settings.author_daily_budget_usd: float | None` (default `100.0`), `_author_daily_budget_from_env() -> float | None`
- Produces: `WorkerContext.author_daily_budget_usd: float | None = 100.0`

- [ ] **Step 1: Write the failing tests** (append to `worker/tests/test_settings.py`)

```python
from worker.settings import _author_daily_budget_from_env


def test_author_budget_defaults_to_100(monkeypatch):
    monkeypatch.delenv("REVA_AUTHOR_DAILY_BUDGET_USD", raising=False)
    assert _author_daily_budget_from_env() == 100.0


def test_author_budget_zero_disables(monkeypatch):
    monkeypatch.setenv("REVA_AUTHOR_DAILY_BUDGET_USD", "0")
    assert _author_daily_budget_from_env() is None
    monkeypatch.setenv("REVA_AUTHOR_DAILY_BUDGET_USD", "-5")
    assert _author_daily_budget_from_env() is None


def test_author_budget_override(monkeypatch):
    monkeypatch.setenv("REVA_AUTHOR_DAILY_BUDGET_USD", "250")
    assert _author_daily_budget_from_env() == 250.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `worker/.venv/bin/python -m pytest worker/tests/test_settings.py -v`
Expected: `ImportError: cannot import name '_author_daily_budget_from_env'`.

- [ ] **Step 3: Implement settings**

In `worker/worker/settings.py`, after the `daily_budget_usd` field:

```python
    # Rolling 24-hour review spend cap per PR author (USD). Reviews by an author
    # whose completed reviews reached this in the trailing 24 hours are declined.
    # None = no per-author cap. Default 100; REVA_AUTHOR_DAILY_BUDGET_USD <= 0 disables.
    author_daily_budget_usd: float | None = 100.0
```

In `from_env`, right after the `daily_budget_usd=(...)` argument:

```python
            author_daily_budget_usd=_author_daily_budget_from_env(),
```

At module bottom, next to `_verify_findings_default_from_env`:

```python
def _author_daily_budget_from_env() -> float | None:
    """REVA_AUTHOR_DAILY_BUDGET_USD: unset = 100; a value <= 0 disables the cap."""
    raw = os.environ.get("REVA_AUTHOR_DAILY_BUDGET_USD")
    if raw is None or raw.strip() == "":
        return 100.0
    value = float(raw)
    return value if value > 0 else None
```

Also reword the `daily_budget_usd` comment in the dataclass to: `# Rolling 24-hour cap (USD) for non-review Claude spend (audits, replies, tickets, ...). None = no cap. Review spend is capped per author instead.`

- [ ] **Step 4: Implement context**

In `worker/worker/runner.py`, `WorkerContext`, after `daily_budget_usd: float | None = None`:

```python
    author_daily_budget_usd: float | None = 100.0
```

In `build_worker_context`, after `daily_budget_usd=settings.daily_budget_usd,`:

```python
        author_daily_budget_usd=settings.author_daily_budget_usd,
```

- [ ] **Step 5: Compose and docs**

`docker-compose.prod.yml` and `docker-compose.yml`, worker `environment:` block, replace the two existing budget lines with:

```yaml
      # Optional rolling 24-hour cap (USD) on NON-review Claude spend; unset = no cap.
      REVA_DAILY_BUDGET_USD: ${REVA_DAILY_BUDGET_USD:-}
      # Rolling 24-hour review spend cap per PR author (USD). 0 disables.
      REVA_AUTHOR_DAILY_BUDGET_USD: ${REVA_AUTHOR_DAILY_BUDGET_USD:-100}
```

(`docker-compose.yml` has no `REVA_DAILY_BUDGET_USD` line today; add both lines after `REVA_CODEGRAPH_ENABLED`.)

`.env.example` lines 49-52 become:

```
# --- Spend caps (optional) ----------------------------------------------------
# Rolling 24-hour cap (USD) on non-review Claude spend (audits, replies, ticket
# analysis, ...). Unset = no cap.
# REVA_DAILY_BUDGET_USD=25
# Rolling 24-hour review spend cap per PR author (USD). Default 100; 0 disables.
# REVA_AUTHOR_DAILY_BUDGET_USD=100
```

`README.md` line 337: replace the `REVA_DAILY_BUDGET_USD` row and add one after it:

```
| `REVA_DAILY_BUDGET_USD` | — | _(off)_ | Rolling 24-hour cap on **non-review** Claude spend (audits, replies, ticket analysis, change notes, ...); new calls are declined (not run) once trailing spend reaches it. Serialized via a Postgres advisory lock; overshoot bounded by concurrent workers |
| `REVA_AUTHOR_DAILY_BUDGET_USD` | — | `100` | Rolling 24-hour PR-review spend cap per PR author (all repos). Reviews by an author at the cap are declined with a Check Run naming the author. `0` disables |
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `worker/.venv/bin/python -m pytest worker/tests/test_settings.py -v && docker compose -f docker-compose.prod.yml config --quiet && docker compose config --quiet`
Expected: tests PASS; both compose files validate (the `config` calls may warn about unset secrets; only a non-zero exit is a failure).

---

### Task 3: Runner gates — per-author decline, global exclusion, in-review follow-ups

**Files:**
- Modify: `worker/worker/runner.py:318-325` (pre-flight call), `:372-409` (gates), `:421-426` (`verify_budget_ok`), `:514-519` (delta resolution gate)
- Modify: `reva/db/repo_lookup.py:28-46` (`get_pr_basic` returns `author_login`)
- Test: `worker/tests/test_runner.py:600-616`, `:677-687`, `:804-813`
- Test: `worker/tests/test_audit_tasks.py:195-211`, `worker/tests/test_comment_reply.py:83-92` (seed non-review kinds)

**Interfaces:**
- Consumes: `writers.sum_author_review_cost_since`, `writers.sum_estimated_cost_since(..., exclude_kinds=...)` (Task 1); `WorkerContext.author_daily_budget_usd` (Task 2)
- Produces: `author_budget_exceeded(ctx, pull_request_id) -> float | None`; `_budget_decline_if_exceeded(ctx, params, log) -> ReviewResult | None` (new `params` argument)

- [ ] **Step 1: Rewrite the existing budget tests in `worker/tests/test_runner.py`**

Replace `_set_budget`, `test_review_declined_when_over_budget`, `test_budget_declined_review_enqueues_nothing` and `test_verify_budget_ok_passed_true_under_budget` with:

```python
def _set_budget(s, budget):
    import dataclasses
    from worker.runner import set_context
    set_context(dataclasses.replace(s["ctx"], author_daily_budget_usd=budget))


def _seed_author_spend(s, cost: float, *, author: str = "alice", pr_number: int = 77) -> None:
    """A completed, paid review by `author` on another PR (the per-author cap's source)."""
    from reva.db.models import ReviewRun
    pr_id = writers.upsert_pull_request(
        s["db"], repository_id=s["repo_id"], github_pr_id=9000 + pr_number,
        pr_number=pr_number, title="Prior", author_login=author, base_branch="main",
        head_branch="feat/prior", head_sha=f"prior{pr_number}", state="closed", draft=False,
    )
    with s["db"].session() as db_s:
        db_s.add(ReviewRun(
            repository_id=s["repo_id"], pull_request_id=pr_id, head_sha=f"prior{pr_number}",
            status="completed", trigger_event="opened", review_mode="diff",
            estimated_cost_usd=cost, completed_at=datetime.now(timezone.utc),
        ))


def test_review_declined_when_author_over_budget(ctx_and_fakes):
    s = ctx_and_fakes
    _seed_author_spend(s, 5.0)  # alice already spent $5 on another PR
    _set_budget(s, 1.0)
    s["reviewer"].result = _completed_result()

    out = run_review(_params(s))

    assert out["status"] == "declined"
    assert "@alice" in out["decline_reason"]
    assert "$1" in out["decline_reason"]
    assert s["reviewer"].call_count == 0           # paid review never ran
    assert len(s["github"].created_pr_reviews) == 0
    assert len(s["github"].created_issue_comments) == 1  # decline posted


def test_review_runs_when_other_author_over_budget(ctx_and_fakes):
    s = ctx_and_fakes
    _seed_author_spend(s, 5.0, author="bob")
    _set_budget(s, 1.0)
    s["reviewer"].result = _completed_result()

    out = run_review(_params(s))

    assert out["status"] == "completed"
    assert s["reviewer"].call_count == 1


def test_review_ignores_global_cap(ctx_and_fakes):
    # Review spend is capped per author only; the global cap guards non-review calls.
    s = ctx_and_fakes
    writers.record_claude_spend(s["db"], "review", 50.0)
    set_context(replace(s["ctx"], daily_budget_usd=1.0, author_daily_budget_usd=100.0))
    s["reviewer"].result = _completed_result()

    out = run_review(_params(s))

    assert out["status"] == "completed"


def test_review_runs_without_author_login(ctx_and_fakes):
    s = ctx_and_fakes
    writers.upsert_pull_request(
        s["db"], repository_id=s["repo_id"], github_pr_id=9001, pr_number=42,
        title="Add foo", author_login=None, base_branch="main", head_branch="feat/foo",
        head_sha="deadbeef", state="open", draft=False,
    )
    _set_budget(s, 1.0)
    s["reviewer"].result = _completed_result()

    out = run_review(_params(s))

    assert out["status"] == "completed"


def test_review_runs_when_author_cap_disabled(ctx_and_fakes):
    s = ctx_and_fakes
    _seed_author_spend(s, 500.0)
    _set_budget(s, None)
    s["reviewer"].result = _completed_result()

    out = run_review(_params(s))

    assert out["status"] == "completed"


def test_budget_declined_review_enqueues_nothing(ctx_and_fakes):
    # Over-budget declines happen before the claim commits to a review cycle —
    # no review_started, so Odoo's reviewed badge is left untouched.
    s = ctx_and_fakes
    queue = MagicMock()
    set_context(replace(s["ctx"], rq_queue=queue, author_daily_budget_usd=1.0))
    _seed_author_spend(s, 5.0)

    run_review(_params(s))

    queue.enqueue.assert_not_called()


def test_verify_budget_ok_passed_true_under_budget(ctx_and_fakes):
    # execute() receives verify_budget_ok from the per-author pre-flight check.
    s = ctx_and_fakes
    _set_budget(s, 1000.0)
    s["reviewer"].result = _completed_result()
    run_review(_params(s))
    assert s["reviewer"].last_verify_budget_ok is True


def test_verify_budget_ok_false_when_author_reaches_cap_after_preflight(ctx_and_fakes):
    # Spend landing between the pre-flight gate and execute() must switch the
    # self-critique off without declining the already-claimed review.
    s = ctx_and_fakes
    _set_budget(s, 1.0)
    s["reviewer"].result = _completed_result()
    from worker import runner as runner_mod
    real = runner_mod.author_budget_exceeded
    calls = {"n": 0}

    def flip(ctx, pr_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else 5.0  # pre-flight passes, later calls over

    with patch.object(runner_mod, "author_budget_exceeded", side_effect=flip):
        run_review(_params(s))
    assert s["reviewer"].last_verify_budget_ok is False
    assert real is not None
```

Make sure `datetime`, `timezone`, `replace`, `MagicMock`, `patch` are imported at the top of `test_runner.py` (they are, except possibly `patch`: `from unittest.mock import MagicMock, patch`).

Update the two non-review tests so they seed a **non-review** kind (they must keep tripping the global cap):

`worker/tests/test_audit_tasks.py:199`: `writers.record_claude_spend(d, "audit", 50.0)  # already over the cap`
`worker/tests/test_comment_reply.py:84`: `writers.record_claude_spend(db_with_finding, "reply", 50.0)`
and the comment on line 91 becomes `# no reply spend added beyond the seeded reply row`.

Add one regression test to `worker/tests/test_audit_tasks.py` after `test_run_audit_declines_when_over_budget_without_running`:

```python
def test_run_audit_ignores_review_spend_in_global_cap(db):
    """Review spend is capped per author; the global cap must not count it."""
    d, repo_id = db
    writers.record_claude_spend(d, "review", 50.0)
    writers.record_claude_spend(d, "delta_verify", 5.0)
    auditor = FakeAuditor(_result(cost=3.5))
    set_context(_ctx(d, auditor, budget=10.0))

    out = run_audit({"repository_id": repo_id, "installation_id": 500})

    assert out["status"] != "declined"
    assert auditor.called is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `worker/.venv/bin/python -m pytest worker/tests/test_runner.py -k "budget or author" worker/tests/test_audit_tasks.py -k "budget or review_spend" -v`
Expected: `test_review_declined_when_author_over_budget`, `test_budget_declined_review_enqueues_nothing`, `test_verify_budget_ok_false_when_author_reaches_cap_after_preflight` (AttributeError on `author_budget_exceeded`), `test_review_ignores_global_cap` and `test_run_audit_ignores_review_spend_in_global_cap` FAIL; the rest pass.

- [ ] **Step 3: Implement the gates in `worker/worker/runner.py`**

Replace `budget_exceeded` and `_budget_decline_if_exceeded` (lines 372-409) with:

```python
# Ledger kinds that are PR-review spend — capped per author, not globally.
_REVIEW_SPEND_KINDS = ("review", "delta_verify")


def budget_exceeded(ctx: WorkerContext) -> float | None:
    """Rolling 24h NON-review spend (USD) if the global cap is reached, else None.

    Callers use this to decline a NEW non-review Claude call (audit, reply,
    ticket analysis, change note, ...) when the cap is full; in-flight calls are
    never interrupted. PR-review spend is excluded here and capped per author
    (author_budget_exceeded).
    """
    if ctx.daily_budget_usd is None:
        return None
    spent = writers.sum_estimated_cost_since(
        ctx.db, datetime.now(timezone.utc) - timedelta(days=1),
        serialize=True, exclude_kinds=_REVIEW_SPEND_KINDS,
    )
    return spent if spent >= ctx.daily_budget_usd else None


def author_budget_exceeded(ctx: WorkerContext, pull_request_id: int) -> float | None:
    """Rolling 24h review spend (USD) of the PR's author if the per-author cap
    is reached, else None. None also when the cap is off or the PR has no
    author login (nothing to attribute to)."""
    if ctx.author_daily_budget_usd is None:
        return None
    spent = writers.sum_author_review_cost_since(
        ctx.db, pull_request_id, datetime.now(timezone.utc) - timedelta(days=1),
        serialize=True,
    )
    if spent is None:
        logger.info("author_budget_skipped_no_author", pull_request_id=pull_request_id)
        return None
    return spent if spent >= ctx.author_daily_budget_usd else None


def instance_budget_exceeded(ctx: WorkerContext, odoo_instance_id: int) -> float | None:
    ...  # unchanged, keep as is


def _budget_decline_if_exceeded(ctx: WorkerContext, params: JobParams, log) -> ReviewResult | None:
    """Return a declined ReviewResult if the PR author's rolling 24h review
    spend cap is reached, else None."""
    spent = author_budget_exceeded(ctx, params.pull_request_id)
    if spent is None:
        return None
    author = repo_lookup.get_pr_basic(ctx.db, params.pull_request_id).get("author_login")
    log.warning(
        "review_over_author_budget", author=author,
        spent_usd=round(spent, 2), budget_usd=ctx.author_daily_budget_usd,
    )
    reason = (
        f"REVA's rolling 24-hour review budget for @{author} "
        f"(${ctx.author_daily_budget_usd:.0f}) has been reached (≈${spent:.0f} spent). "
        f"Reviews resume automatically as spend rolls off."
    )
    return ReviewResult(status="declined", summary="Author's daily review budget reached.",
                        risk_level="low", decline_reason=reason)
```

`repo_lookup.get_pr_basic` (`reva/db/repo_lookup.py:28-46`) does not return the author yet. Add `PullRequest.author_login` as a fifth column to its `select(...)` and `"author_login": row[4],` to the returned dict (the `Reviewer` also calls it through `DatabaseRepoLookup`; an extra key is harmless there). `logger` is the module-level structlog logger already used in `runner.py`.

Update the three call sites:

1. Line ~320: `budget_decline = _budget_decline_if_exceeded(ctx, params, log)` and change the log line to `log.info("review_job_done", status="declined", reason="over_author_budget")`.
2. Line ~425: `result = ctx.reviewer.execute(params, verify_budget_ok=author_budget_exceeded(ctx, params.pull_request_id) is None)` and update the comment above it: `# Pre-flight budget gate for the optional second-pass self-critique: don't start paid verification when the author's rolling cap is already reached.`
3. Line ~516: `if author_budget_exceeded(ctx, params.pull_request_id) is None:` with the comment above changed to `# ...each candidate is a paid verifier call, so skip when the author's rolling cap is blown.`

- [ ] **Step 4: Run the worker suite**

Run: `worker/.venv/bin/python -m pytest worker/tests -q`
Expected: all PASS. If a test outside the ones edited above still seeds `"review"` ledger rows to trip the global cap, switch its kind to the kind of the path under test (`audit`, `reply`, `ticket_analysis`, ...) — never weaken the assertion.

- [ ] **Step 5: Lint and full definition of done**

Run: `ruff check reva worker/worker api/app scheduler/scheduler && make test`
Expected: ruff clean; worker, api and scheduler suites green (shared `reva/db/writers.py` changed).

- [ ] **Step 6: Report**

State plainly: unit-tested on SQLite only. The advisory-lock branch and the `NOT IN` on Postgres are exercised only via `make test-integration` or the first prod boot. No commit — Joseph commits.
