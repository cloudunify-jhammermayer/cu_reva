# Per-author review budget — design

Status: DESIGN · Date: 2026-09-22 · Repo: cu_reva only (no Odoo contract change).

## Need

On 2026-09-21/22 one developer opened 24 PRs on `hackmair-platzhirsch` and 12 on `Aurium-Systems` in one day, each
with an immediate re-review comment. Their reviews spent ~$75 of the $100 global cap, so every other developer's
reviews were declined that evening. Decisions (Joseph, 2026-09-22):

- PR reviews are capped **per PR author**, default **$100 per rolling 24 hours**, one number for everyone.
- The global cap `REVA_DAILY_BUDGET_USD` keeps guarding the **non-review** Claude calls only (audits, comment
  replies, ticket analysis, ticket issues, timesheet review, support answers, change notes, learned memory).
  Review spend no longer counts against it.

## Behaviour

1. **Review pre-flight** (`worker/worker/runner.py`, `run_review`): before claiming a paid review, REVA sums the
   author's paid review spend (regardless of run status) across all repos in the trailing 24 hours. At or above the
   cap the run is declined with a Check Run, exactly like today's global decline:

   > REVA's rolling 24-hour review budget for @`<login>` ($100) has been reached (≈$X spent). Reviews resume
   > automatically as spend rolls off.

   Rows in `review_runs` get `status = declined` with that `decline_reason`, so the Reviews tab in the TUI shows it.
2. **In-review paid follow-ups** use the same per-author gate instead of the global one, because they are review
   spend: the optional self-critique pass (`verify_budget_ok` handed to `Reviewer.execute`) and the delta finding
   resolution after posting (`_verify_and_resolve_findings`). When the author is over the cap mid-run, those steps
   are skipped, the main review still completes.
3. **Global gate** (`budget_exceeded`) excludes the ledger kinds `review` and `delta_verify`. All its other callers
   (audit, reply, change note, learned memory, support escalation) are untouched.
4. **PR without an author login** (`pull_requests.author_login IS NULL`): the per-author gate is skipped and the
   review runs. Logged at info; no ops event, this is not a degradation.
5. **Disabling**: `REVA_AUTHOR_DAILY_BUDGET_USD=0` (or any value ≤ 0) turns the per-author cap off. Unset means the
   default of 100.

Explicit triggers (comment, requeue) are gated like any other run; the cap is about spend, not idempotency.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `REVA_AUTHOR_DAILY_BUDGET_USD` | `100` | Rolling 24-hour review spend cap per PR author (USD). `0` disables. |
| `REVA_DAILY_BUDGET_USD` | _(off)_ | Rolling 24-hour cap for **non-review** Claude spend (changed meaning). |

Prod currently has `REVA_DAILY_BUDGET_USD=200`; after this ships it covers ~$0.30/day of non-review spend and can
be lowered.

## Implementation

- `worker/worker/settings.py`: parse `REVA_AUTHOR_DAILY_BUDGET_USD` (default 100.0, ≤ 0 → `None`).
  `WorkerContext` gets `author_daily_budget_usd: float | None = 100.0`; `get_context` passes it through.
- `reva/db/writers.py`:
  - `sum_estimated_cost_since(db, since, *, serialize=False, exclude_kinds=())` — new keyword, filters
    `ClaudeSpend.kind NOT IN exclude_kinds`.
  - `sum_author_review_cost_since(db, pull_request_id, since, *, serialize=False) -> float | None` — returns
    `None` when the PR has no `author_login`, else the sum of `review_runs.estimated_cost_usd` joined through
    `pull_requests.author_login` for paid runs (`estimated_cost_usd > 0`) regardless of status, because
    `review_runs` rows are upserted per (repo, pr, sha, mode) and a later decline/failure on the same SHA reuses
    the paid row, with `completed_at >= since`. Under `serialize=True` takes the same advisory lock
    (`_BUDGET_ADVISORY_LOCK_KEY`) as the global gate.
- `worker/worker/runner.py`:
  - `budget_exceeded(ctx)` passes `exclude_kinds=("review", "delta_verify")`.
  - New `author_budget_exceeded(ctx, pull_request_id) -> float | None`: `None` when the cap is off, the PR has no
    author, or spend is below the cap; else the spent amount.
  - `_budget_decline_if_exceeded(ctx, params, log)` uses `author_budget_exceeded`; the decline text names the login.
  - The `verify_budget_ok=` argument and the `delta_resolution_budget_skip` check use `author_budget_exceeded`.
- `docker-compose.prod.yml` + `docker-compose.yml` (worker service): pass `REVA_AUTHOR_DAILY_BUDGET_USD` through
  with default `100`.
- `.env.example`, `README.md` env table: new row; reword the `REVA_DAILY_BUDGET_USD` row to "non-review spend".
- No migration, no new table, no API or TUI change (declined runs are already listed with their reason).

Roughly 80 lines of Python plus tests.

## Tests

`worker/tests/test_runner.py` (existing `ctx_and_fakes` fixture, `_set_budget` helper extended with the author cap):

- author at cap → run declined, `decline_reason` names the login, reviewer not called, nothing enqueued.
- same spend on another author → review runs.
- global cap at $1 with $50 of `review` ledger rows → review still runs (review spend excluded from the global gate);
  a `reply`/`audit` gate with the same ledger still trips on non-review rows.
- PR with `author_login = None` → review runs.
- `author_daily_budget_usd=None` → gate off.
- `verify_budget_ok` is `False` when the author is over the cap after the pre-flight passed (spend inserted between).

`worker/tests/test_settings.py`: default 100 when unset, `0` → `None`,
`REVA_AUTHOR_DAILY_BUDGET_USD=250` → 250.0.

Writers test for `sum_author_review_cost_since` (new `worker/tests/test_author_budget_writers.py`, SQLite in-memory like `test_instance_quota_writers.py`): sums across repos for the same login, counts paid runs regardless of
status (a declined/failed row that reused a paid run's row still counts), ignores unpaid runs and runs older than
`since`, returns `None` for a PR without author.

## Out of scope

- Attributing `delta_verify` and `triage` ledger rows to the author (cents, Haiku-priced, bounded; the per-author
  sum reads `review_runs`, not the ledger).
- Same-SHA re-reviews under-count per author (the upserted row keeps only the last cost); the real fix is a
  `pull_request_id` column on the append-only `claude_spend` ledger (migration), deferred.
- Per-user overrides (a table of logins with individual caps).
- A per-author spend panel in the TUI. Declined runs already show the reason in the Reviews tab.
- Re-queuing reviews that were declined under the cap once spend rolls off (today's behaviour, unchanged).
