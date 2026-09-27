# Budget wait-and-resume — Design

- **Date:** 2026-09-27
- **Status:** draft, awaiting Joseph's review
- **Context:** Joseph, 2026-09-27: "if the budget is gone, reva throws an
  error, which is fine. but it should periodically check if it got budget and
  then continue it." Scope confirmed: all three cap families (Odoo instance
  cap, per-author PR-review cap, global non-review cap).

## Problem

Every budget gate is terminal today. The rolling 24-hour caps free up on
their own as old spend rolls off, but nothing ever re-checks:

| Gate | Where | Today |
|------|-------|-------|
| Odoo instance cap | `ticket_runner`, `support_runner._produce_answer`, `ticket_issue_runner`, `timesheet_runner` | row `failed` + `PermanentError`; Odoo shows the error |
| Per-author review cap | `runner.run_review` | `declined` run, neutral Check Run + PR comment claiming "reviews resume automatically" (only true on the next push) |
| Global non-review cap | `audit_tasks.run_audit`, `reply_runner`, `change_note_runner` | silently skipped (`declined` / return / `skipped_budget`) |

The work is lost until a human resubmits.

Out of scope, unchanged: `memory_distill_runner` (the scheduler re-triggers it
on its own interval anyway) and the support-answer code-grounding escalation
gate (it degrades to a docs-only answer, it does not fail).

## Design

### Mechanism: the job defers itself through RQ

The worker already runs `Worker.work(with_scheduler=True)` (needed for
`Retry` intervals), so `queue.enqueue_in()` works today with no new process.
A gated job that finds its cap full does not fail; it re-enqueues **itself**
with the same task path and params for `REVA_BUDGET_RETRY_SECONDS` later and
returns `{"status": "waiting_budget"}`. On the next run the gate is checked
again; still full → defer again, until `REVA_BUDGET_WAIT_MAX_SECONDS` after
the first wait, then the job takes today's terminal path (fail / decline /
skip) so nothing waits forever when other work keeps the cap full.

Why not a scheduler loop over "waiting" DB rows: the scheduler would have to
rebuild every job type's params from its row (the API requeue routes do that
per type today), i.e. move four param builders into `reva/` and add a fifth
loop. Self-deferral is one helper and one call per gate.

### Shared helper (`worker/runner.py`)

```python
def defer_for_budget(ctx, job_params: dict, *, kind: str, spent: float,
                     cap: float, log) -> dict | None:
```

- Reads `budget_wait_since` from `job_params` (absent → now = first wait).
- If `now - budget_wait_since >= REVA_BUDGET_WAIT_MAX_SECONDS` → returns
  `None`; the caller falls through to its existing terminal path.
- Else `ctx.rq_queue.enqueue_in(timedelta(seconds=REVA_BUDGET_RETRY_SECONDS),
  <current job's func_name>, job_params | {"budget_wait_since": ...},
  job_timeout=<current job's timeout>, retry=<shared Retry(max=3, ...)>,
  failure_ttl=<current job's>)`. Task path, timeout and failure TTL come from
  `rq.get_current_job()`, so every gate defers with the policy it was enqueued
  with.
- Returns `{"status": "waiting_budget", "kind": kind, "spent_usd": ...,
  "retry_job_id": ..., "budget_wait_since": ...}`.
- `REVA_BUDGET_RETRY_SECONDS <= 0`, `ctx.rq_queue is None`, or an enqueue
  failure → returns `None` (today's behaviour) and records an ops event
  `budget_wait_enqueue_failed` (error) for the failure case.
- Ops events (`record_ops_event`): `budget_wait_started` (warning, on the
  first deferral only) and `budget_wait_expired` (error, on give-up). Repeat
  checks only log, so ten waiting tickets do not produce ~1000 events a day.

Settings: `REVA_BUDGET_RETRY_SECONDS` (default 900) and
`REVA_BUDGET_WAIT_MAX_SECONDS` (default 86400) on `worker/settings.py` →
`WorkerContext`. Documented in the worker README and `.env.example`.

`budget_wait_since: datetime | None = None` is added to `JobParams`,
`TicketJobParams`, `SupportJobParams`, `TicketIssueJobParams`,
`TimesheetJobParams`, `AuditJobParams`. Reply and change-note jobs use plain
dicts and carry the key as-is. The API's `TicketJobParams`-shaped request
schemas are untouched (the field is worker-internal and defaults to None).

### Accept-time gate (deviation, Joseph's call)

`assert_instance_within_budget` answers 429 at submit when an instance cap is
full, so the request never becomes a row and nothing can resume. With waiting
on (`REVA_BUDGET_RETRY_SECONDS > 0`) the api accepts (202) and the worker gate
parks the job; with waiting off the 429 stays. Plan Task 7; drop it to keep
today's submit-time 429.

### Per gate

**Odoo instance cap** (ticket analysis, support answer, ticket issues,
timesheet review). Row stays `pending` (dedup partial indexes and the Odoo
GET-status contract keep working unchanged); a new nullable column
`budget_wait_since TIMESTAMPTZ` on `ticket_analyses`, `support_turns`,
`ticket_issue_runs`, `timesheet_review_runs` is set on the first wait and
cleared when the paid work starts. Migration `050_budget_wait_since.sql`
(`ADD COLUMN IF NOT EXISTS` ×4) + ORM models. On give-up: today's code path
exactly (`record_*_failed`, failed callback where one exists,
`PermanentError`).

`_is_stale_pending` in the four API routes exempts a row whose
`budget_wait_since` is younger than `REVA_BUDGET_WAIT_MAX_SECONDS` + the
route's stale window, so an ops/Odoo requeue cannot start a second paid run
beside the scheduled one. Requeue of such a row answers 409
"waiting for budget; retries automatically".

Ticket issues: the gate sits after the reconcile branch; deferral happens
before `plan_with_response`, before any callback. Timesheet: the gate is
before the chunk loop; already-processed lines are skipped on re-run today
(`get_timesheet_line_ids`), so a partial run resumes correctly.

**Per-author review cap.** On wait: `review_runs.status = "waiting_budget"`
(new status; `summary` carries the reason) and a Check Run in state `queued`
titled "Waiting for review budget" via `_post_simple_check_run` — no PR
comment (no spam). The run row is claimed already; a `waiting_budget` row is
re-claimable (`claim_review_run` re-claims any non-`running` row) and:

- `is_already_posted` treats `waiting_budget` like `failed` (a queued check is
  not a review), so the deferred job is not skipped.
- On re-claim of a `waiting_budget` row, `reset_review_run_post_state` runs as
  for the explicit re-review path; `_check_run_id_or_recover` then finds the
  queued check by SHA and updates it in place with the real result.
- The stale reaper only sweeps `running`, so waiting rows are not reaped.
- If the author pushes meanwhile, the deferred job runs against the old SHA
  and the existing head-moved check yields `stale` (no paid call). The new SHA
  goes through the normal debounce.
- Give-up: today's path (`record_review_declined` + `_post_declined`, which
  updates the queued check to neutral and posts the comment). The decline
  text loses "Reviews resume automatically as spend rolls off" and says the
  wait expired instead.

**Global non-review cap.**

- Audit: defer `run_audit` with its `AuditJobParams` (no row exists yet, as
  today). Give-up: today's `{"status": "declined", "reason": "over_budget"}`.
- Comment reply: defer the reply job dict. Give-up: today's silent return
  (plus the `budget_wait_expired` ops event, which today's path lacks).
- Change note: on the first over-budget note, defer the whole job dict and
  return; notes stay `pending`, so `maybe_deliver_change_notes` delivers
  once the budget frees (delivery is all-notes-done today). Give-up: today's
  `skipped_budget` per note, then delivery of the rest.

### API and TUI

- `GET /api/v1/ticket-analyses`, support requests, ticket issues, timesheet
  reviews: expose `budget_wait_since` in the list/detail schemas.
- Reviews list already exposes `status`; `waiting_budget` is a new value.
- TUI: tickets tab renders "waiting for budget since HH:MM" for rows with the
  column (analysis, issue run, support turn); reviews tab accepts
  `waiting_budget` in the status filter cycle and shows it with the pending
  style; the requeue keybinding's existing guard (only failed/stale/declined/
  completed) already refuses it. `internal/api/{types,mock}.go` gain the field.
  `go build/vet/test ./...` stays green.
- Ops events above surface in the Failures tab as usual.

### `/reviews` status page

A small browser page for consultants, alongside `/docs`: **what is waiting for
budget, and how full each budget is.** Same trust model as the docs site: no
app-layer auth, gated at the edge by Cloudflare Access (ops step: add the
`/reviews` path prefix to the existing Access application; documented in
`docs/setup-production.md` and the `docs-ui/README.md` diagram).

Served by the **api** (no Node build, no second SPA): one router
`api/app/routes/budget_status.py` mounted at `/reviews`:

- `GET /reviews/` → a single static HTML file (`api/app/static/reviews.html`,
  vanilla JS, no framework) that fetches the JSON below and re-fetches every
  60 s.
- `GET /reviews/data` → JSON:
  - `budgets.global`: non-review spend in the last 24 h, cap
    (`REVA_DAILY_BUDGET_USD`, null = off), remaining.
  - `budgets.instances[]`: per Odoo instance — name, spend 24 h, cap,
    remaining (`sum_instance_cost_since` per instance).
  - `budgets.authors[]`: per PR author with paid reviews in the last 24 h —
    login, spend, cap (`REVA_AUTHOR_DAILY_BUDGET_USD`), `over: bool`. New query
    `review_cost_by_author_since` (group `review_runs` by
    `pull_requests.author_login`, same join as `sum_author_review_cost_since`).
  - `waiting.reviews[]`: grouped **per repository** (stacked PRs put 10
    waiting reviews in one repo; a flat list clusters): each group carries
    repo full name, PR count, distinct authors, oldest `waiting_since`, and
    `prs[]` with PR number, title, author, mode, waiting since. The page
    renders one collapsible panel per repo (`<details>`), collapsed by
    default with the summary line, click to list the PRs; a repo's open state
    is kept in `localStorage` across the 60 s refresh.
  - `waiting.odoo[]`: rows with `budget_wait_since` set across
    `ticket_analyses`, `support_turns`, `ticket_issue_runs`,
    `timesheet_review_runs` — kind, instance name, record (`model_name` +
    `ticket_id`), waiting since.
  - `waiting.jobs[]`: deferred jobs that have no row (audit, comment reply,
    change note): read from RQ's `ScheduledJobRegistry` on
    `app.state.rq_queue`, filtered to jobs whose params carry
    `budget_wait_since` — kind (from the task path), waiting since, next run
    at. Job args are never echoed (they hold ticket text).
  - `settings`: retry interval and max wait, so the page can say
    "re-checked every 15 min, gives up after 24 h". The api reads the same two
    env vars (`api/app/settings.py`), defaults identical to the worker's.

The api needs no new dependency: it already holds the DB and the RQ queue.
nginx: `location /reviews/` proxied to the api under the `api` rate-limit
zone, exactly like `/repo-docs/`; the catch-all `location /` stays 404.

The page shows only what an internal consultant may see anyway (repo names, PR
numbers, author logins, instance names, ticket ids, dollar figures). It is
read-only: no requeue or cap edits from the browser (those stay in the TUI).

Tests: route tests on SQLite with a fake `ScheduledJobRegistry` — empty state,
one waiting row per table, an author over cap, a deferred audit job; HTML
route returns 200 `text/html`.

### Docs

Worker README (budget section), `docs/` budget/ops notes where the caps are
described, `docs/setup-production.md` (Cloudflare Access prefix `/reviews`,
nginx location) and the `docs-ui/README.md` diagram. `contracts/` is **not**
regenerated: no Odoo-facing contract changes (status values and callbacks
unchanged).

## Testing

Unit tests per service, SQLite + fake RQ queue (the existing `rq_queue`
double pattern from the board-status tests):

- `defer_for_budget`: first wait stamps `budget_wait_since` and enqueues with
  the current job's timeout/retry; repeated wait keeps the stamp; expired wait
  returns None; disabled setting / no queue / enqueue failure return None with
  the ops event.
- One test per gate: over-budget → row still pending with the column set,
  deferred job enqueued, no paid call, no failed callback; expired → today's
  assertions still hold (existing tests, adjusted for the stamp).
- Review: waiting run → status `waiting_budget`, queued check posted, no
  comment; deferred run under budget → completes and updates the same check;
  `is_already_posted` false for waiting; poller-side already-reviewed skip
  unaffected (it only sees consumed pending rows).
- API: `_is_stale_pending` exemption; requeue 409 on waiting rows; schemas
  carry the field.
- TUI: `go test ./...` with the mock client returning a waiting row.

Not unit-testable: real RQ `enqueue_in` scheduling (the worker's
`with_scheduler=True` moves scheduled jobs to the queue) and the Postgres
migration. Validate on the first staging boot: set a tiny instance cap,
submit a ticket, watch the job re-fire and complete after the interval.

## Risks / open

- **Odoo sees "in progress" for up to 24 h** while a ticket waits; no
  callback tells it why. Today it sees an error immediately. If that is worse
  for consultants, the max wait can be lowered per deployment.
- **Redis holds the deferred job's args** (ticket text, base64 images) for the
  whole wait instead of seconds. Prod Redis is `noeviction`; many waiting
  image-heavy tickets consume headroom. Mitigation if it bites: strip `images`
  from the deferred params (a requeue already runs image-blind, see
  `ticket_analyses.image_count`).
- **A waiting job survives Redis loss?** No: scheduled jobs live only in
  Redis. After a Redis flush the row stays `pending` with `budget_wait_since`
  set; the stale-pending exemption expires after max wait + stale window and
  ops can requeue as today.
- **`/reviews` is only as private as the Access app.** Until the `/reviews`
  prefix is added there, the page is reachable by anyone with the hostname
  (same gap the docs site had until 2026-08-07). Deploy the nginx location and
  the Access prefix in the same change.
- Per-author cap wait means an author whose budget is full at 09:00 gets
  reviews again ~24 h later at the earliest, in 15-minute checks. Cheap, but
  the queued Check Run sits on the PR the whole time.
