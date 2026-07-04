# Config & performance hardening batch — design

**Date:** 2026-07-04
**Component:** `cu_reva` — compose files, `db/migrations` + ORM, `scripts/`, `reva/` (claude_code_runner, finding_verifier, ticket_tool, ticket_issue_planner, db writers), `api/` (Dockerfile, ratelimit, instance routes), `scheduler/` (retention loop), `.env.example`, `tui/` (Odoo tab), `docs/setup-production.md`.
**Status:** Design approved (Q&A with Joseph, 2026-07-04), pending implementation plan.
**Implementer:** a different agent executes the plan; this spec is the contract.

## Problem

A 2026-07-04 runtime/config review (docker-compose, RQ, Postgres, Claude-usage,
observability, env sprawl) found four bug-level defects, five cheap
performance/cost wins, config drift, and one missing feature — none of which is
covered by `HANDOFF.md` or `FEATURE_ROADMAP.md` (roadmap items such as the
Tier-4 priority queue, Prometheus, and per-repo budgets were deliberately
excluded; this batch does not duplicate them).

## Context (verified against code, 2026-07-04)

- `REVIEW_JOB_TIMEOUT = 300 + 1500 + 300 = 2100s`
  (`reva/claude_code_runner.py:68-85`), but the prod worker's
  `stop_grace_period` is `1830s` with a comment claiming the timeout is 1800s
  (`docker-compose.prod.yml:193`).
- `ticket_analyses` has no `created_at` index; the list endpoint sorts by it
  (`api/app/queries/ticket_analyses.py:27`). `ticket_issue_runs` has one.
- Every service has a compose healthcheck except the worker.
- Redis: `--maxmemory 256mb --maxmemory-policy noeviction`, container limit
  320M; ticket jobs are enqueued with `failure_ttl = 7d` and carry full request
  payloads (incl. base64 attachments, API body cap 26 MB) in serialized args.
  Ticket/issue requeue endpoints rebuild job params from the DB row, never from
  the Redis payload.
- `ensure_repo` does full `git clone` / `git fetch` (no `--depth`/`--filter`);
  clones are cached per repo and reused (`reva/claude_code_runner.py:174-245`).
- `reva/finding_verifier.py:199,226` sends static system prompts with no
  `cache_control`, up to ~20 Haiku calls per review. The ticket analyzer and
  issue planner do cache theirs.
- One RQ worker replica serves a single queue (`"reviews"`) carrying every job
  type; multi-worker distribution was validated (HANDOFF, 2026-06-14) and
  repo-clone access is serialized by a Postgres advisory lock (`repo_lock`).
- API runs a single uvicorn process (`api/Dockerfile:27`), 2.0-CPU limit.
- Messages-API structured output is forced tool use validated by Pydantic;
  Claude occasionally returns list fields as JSON strings
  (`reva/types.py::_unwrap_json_list` works around it).
- `.env.example` is missing ~18 live tunables (incl. `REVA_VERIFY_MODEL`, all
  scheduler intervals, retention windows) while still showing the deprecated
  `REVA_VERIFY_HIGH_COST`; `REVA_VERIFY_MODEL` is not wired through either
  compose file although its two siblings are.
- The retention loop purges ticket text, ticket-issue text, and
  `github_events`; `claude_spend` grows unbounded (the budget gate needs 24h;
  weekly reports need weeks).
- Stray uncommitted files: `uv.lock` (2026-06-10 uv experiment; migration
  deferred), `reva-prod-fixes.patch`, `reva-tui-cf-access.patch`. The nginx
  template documents that `/docs` + `/repo-docs` must be gated by a Cloudflare
  Access application at the edge — an operator step recorded nowhere else.
- `odoo_instances` (migration 018) has per-instance keys/callbacks and
  per-instance cost is summed off the run tables; there is no per-instance
  budget or rate limit — one misbehaving instance can burn the global
  24h budget (`REVA_DAILY_BUDGET_USD`).

### Locked decisions

1. **Scope**: items A1–A4, B5–B9, C10–C12, D13 below. **uv migration is
   deferred** (own future project); only the stray files are handled.
2. **Quota enforcement at both ends** (API submit-time 429 + worker re-check
   before the paid call). Rate limiting is API-side only.
3. **Worker concurrency via `deploy.replicas: 2`** in prod compose — zero
   code. The roadmap's Tier-4 interactive/batch queue split remains the
   eventual proper fix and is untouched here.
4. Every part is independently shippable; plan order A → B → C → D.
5. `NULL` quota columns mean unlimited — existing instances behave exactly as
   today until an operator sets a cap.

### Explicitly out of scope

- uv/pip migration, CI changes for it.
- Priority/split queues, per-repo budgets, Prometheus, error tracking, OTel
  (roadmap Tiers 4–5).
- Purging `review_runs`/`review_findings`/`audit_*` (feed the 90-day learning
  stats and audit dedup), `admin_audit` (audit trail), `weekly_reports` (tiny).
- TUI editing of quota fields (display-only in v1; set via API).
- Applying `reva-tui-cf-access.patch` (see C12 — surface, don't apply).

## Design

### Part A — bug-level fixes

**A1 — worker stop grace (prod compose).**
`stop_grace_period: 1830s` → `2160s` (= `REVIEW_JOB_TIMEOUT` 2100s + 60s
margin). Replace the stale "Matches the review job timeout (1800s)" comment
with one naming the formula (`LOCK_WAIT_BUDGET + SUBPROCESS_TIMEOUT +
JOB_TIMEOUT_BUFFER` in `reva/claude_code_runner.py`) so the next timeout bump
finds it.

**A2 — `ticket_analyses.created_at` index.**
New migration (**check the next free number first** — the timesheet and
metasoul plans both currently claim 025; whatever ships first wins, the others
renumber): `CREATE INDEX IF NOT EXISTS idx_ticket_analyses_created_at ON
ticket_analyses (created_at);` plus the matching `Index` entry in
`reva/db/models.py::TicketAnalysis.__table_args__` (tests build from models).

**A3 — worker healthcheck.**
New `scripts/worker_healthcheck.py`: connects to `REDIS_URL`, asserts an RQ
worker key for this container's hostname exists (`rq:worker:*` keys carry a
TTL refreshed by the worker's heartbeat, so existence ⇒ alive). Exit 0/1.
Wired in **both** compose files: `interval: 60s`, `timeout: 10s`,
`retries: 3`, `start_period: 60s`.

**A4 — Redis pressure.**
- `_FAILURE_TTL` 7d → 24h in `api/app/routes/v1/ticket_analyses.py` and
  `ticket_issues.py` (requeue never reads the Redis payload; keeping fat
  failed-job args for a week only risks `noeviction` write-rejection).
- Prod compose: `--maxmemory 256mb` → `512mb`, container memory limit
  `320M` → `640M`. `noeviction` and `appendonly yes` unchanged.

### Part B — performance/cost

**B5 — blob-filtered partial clones.**
`ensure_repo` clones with `--filter=blob:none`. Commit history stays complete
(delta-base ancestry checks and `merge-base` keep working); blobs are fetched
on demand at checkout from the same allowlisted GitHub host (egress-overlay
compatible). Existing cached clones are untouched; only new clones (and
re-clones after eviction) get the filter.

**B6 — verifier prompt caching.**
Both static system prompts in `reva/finding_verifier.py` become
cache-controlled blocks (`{"type": "text", "text": …, "cache_control":
{"type": "ephemeral"}}`), matching the ticket-analyzer pattern.
**Acceptance criterion:** on a staging review with ≥3 verified findings,
`cache_read_tokens > 0` on calls 2+. If the prefix is under Haiku's minimum
cacheable length (2048 tokens) the marker is a silent no-op — then remove it
and record the measurement in the commit message instead.

**B7 — two worker replicas (prod).**
`deploy.replicas: 2` on the worker service. Prerequisites enforced by the
plan: the worker service must have **no `container_name`** (incompatible with
replicas); resource limits are per replica (2 × 1G memory budget — confirm
headroom on the prod host before deploy). Cross-replica correctness is
already designed in (Postgres advisory repo locks, atomic run claims,
`is_already_posted` idempotency) and was validated in the 2026-06-14
multi-worker test.

**B8 — API workers.**
`api/Dockerfile`: `uvicorn … --workers 2`. Documented consequence: the
in-memory rate limiter and its sweep become per-process (it is already
best-effort per-instance by design; nginx `limit_req` zones remain the real
gate). No shared state in `app.state` is cross-request-mutable besides the
limiter buckets.

**B9 — strict structured outputs.**
Add `"strict": true` to the tool definitions returned by
`reva/ticket_tool.py::build_ticket_tool_schema`, the issue-planner tool
builder (`reva/ticket_issue_planner.py`), and the finding-verifier tool
builders — and record it as the default for future Messages-API tools
(timesheet, metasoul website analysis). Keep the `_unwrap_json_list`
validators as belt-and-braces. Unit tests assert the flag is present;
schema *acceptance* by the API is a one-call staging live-gate (A1/A2
pattern) since strict mode restricts the JSON-Schema subset.

### Part C — hygiene

**C10 — config surface sync.**
- Rewrite `.env.example`: every `REVA_*` var read by
  `api/app/settings.py`, `worker/worker/settings.py`,
  `scheduler/scheduler/settings.py`, `reva/config.py`, `reva/logging.py`,
  and `api/app/ratelimit.py` appears once, grouped by service, with a
  one-line comment and its default. Remove the deprecated
  `REVA_VERIFY_HIGH_COST` from the example (code keeps honoring it with the
  existing deprecation warning).
- Wire `REVA_VERIFY_MODEL` through both compose files' worker environment
  next to `REVA_DEFAULT_MODEL`/`REVA_DEEP_MODEL`.
- New unit test (worker suite): parse the settings sources for
  `os.environ[...]`/`os.environ.get(...)`/`env_or_file(...)` names matching
  `^REVA_` and assert each appears in `.env.example` (allowlist for
  test-only vars like `REVA_TEST_POSTGRES_URL`). Drift can't silently return.

**C11 — spend retention.**
`reva/db/writers.py::purge_old_claude_spend(db, older_than_days) -> int`
(DELETE, mirroring `purge_old_github_events`), called from the existing daily
retention pass in the scheduler next to the other purges. New
`REVA_SPEND_RETENTION_DAYS`, default **400** (cost dashboards keep >1 year;
the budget gate needs 24h). Documented non-purges: see "Explicitly out of
scope".

**C12 — stray files + CF-Access operator note.**
- Delete `uv.lock` (uv migration deferred by decision).
- For `reva-prod-fixes.patch` and `reva-tui-cf-access.patch`: `git apply
  --check` + read each against current `main`. If superseded/already landed →
  delete. **If either contains unlanded changes, stop and surface it to
  Joseph — do not apply silently.**
- Add the Cloudflare Access operator step (create an Access application
  gating `/docs` + `/repo-docs`; `/webhooks`, `/api`, `/health` stay
  ungated) to the operator checklist in `docs/setup-production.md`, so it
  stops living only in an nginx template comment.

### Part D — per-instance quotas

**DB.** Migration (next free number after A2's) + ORM:

```sql
ALTER TABLE odoo_instances ADD COLUMN IF NOT EXISTS daily_budget_usd NUMERIC(12, 2);
ALTER TABLE odoo_instances ADD COLUMN IF NOT EXISTS rate_limit_per_minute INTEGER;
```

`NULL` = unlimited (existing instances unaffected).

**Spend query.** `reva/db/writers.py::sum_instance_cost_since(db,
odoo_instance_id, since) -> float` — sums `estimated_cost_usd` across the
instance-scoped run tables: `ticket_analyses` + `ticket_issue_runs` today.
The function is the single extension point: when the timesheet and
metasoul-website tables land, they are added to this sum (both pending specs
carry `odoo_instance_id` + `estimated_cost_usd`).

**API enforcement.** In the instance-gated create routes (ticket analysis,
create-issues — and, by the same helper, future instance-scoped creates): if
the resolved instance has `daily_budget_usd` set and
`sum_instance_cost_since(now-24h) >= daily_budget_usd`, respond **429** with
a human-readable detail (Odoo shows it). Shared helper so the pending
timesheet/metasoul routes adopt it with one line. Rate limiting: the existing
rate-limit dependency gains a per-instance branch — when the resolved
instance has `rate_limit_per_minute` set, count against key
`instance:{id}` with that limit (same in-memory, best-effort-per-process
semantics as the global limiter; nginx remains the outer gate).

**Worker enforcement.** New `instance_budget_exceeded(ctx, odoo_instance_id)
-> float | None` (mirrors `budget_exceeded`'s shape). Checked in
`ticket_runner` and `ticket_issue_runner` before the paid call: over-cap →
record failed row, notify Odoo through that path's existing failure channel
(ticket analysis: the row stays failed and Odoo's poll/requeue surface shows
the error; create-issues: `issues_created(status="failed", error=…)`), then
raise `PermanentError`.
Bounded overshoot: in-flight jobs finish, queued ones fail fast. The global
`REVA_DAILY_BUDGET_USD` gate is unchanged (still review/audit paths only —
extending it is not in this batch).

**Admin surface.** `PATCH /api/v1/odoo-instances/{id}` accepts the two new
optional fields (validated ≥ 0 or null), admin-audited like the existing
instance CRUD.

**TUI.** Odoo tab (`0`): two new columns — 24h spend and budget, rendered
`$3.20 / $10` (or `—` when unlimited), styled red at ≥90% of budget.
Backing data: extend the existing instances list endpoint/queries with the
24h spend + budget fields. Display-only; editing stays on the API.

## Error handling summary

| Case | Behavior |
|---|---|
| Instance over daily budget at submit | 429 + detail (no row, no job) |
| Instance over budget at worker (queued backlog) | failed row + failed callback + `PermanentError` (terminal, no retry) |
| Instance over `rate_limit_per_minute` | 429 from the rate-limit dependency |
| Healthcheck can't reach Redis / no worker key | container unhealthy (restart policy applies) |
| `purge_old_claude_spend` failure | same handling as the existing purge calls in the retention pass (logged, next cycle retries) |
| Patch file contains unlanded work (C12) | stop, surface to Joseph — never auto-apply |

## Testing

- **Unit (SQLite + mocks, per repo conventions):** A2 model index present;
  A4 constant change; B9 `strict: true` asserted on every tool builder;
  C10 env-drift test; C11 purge writer (deletes old, keeps new, returns
  count); D writers (`sum_instance_cost_since` across both tables, window
  edges), API 429 paths (budget + rate limit), PATCH validation + audit row,
  worker gate tests in both runners (over-cap → failed + callback + no paid
  call), TUI mock-client rendering of the new columns.
- **Compose/infra items (A1, A3, A4, B7):** `docker compose -f
  docker-compose.prod.yml config` must parse; healthcheck script gets a unit
  test with a fake Redis; replica prerequisites (no `container_name`)
  asserted by grep in the plan.
- **Staging live-gates (A1/A2 pattern, listed for the operator):** B5 one
  full review on a fresh clone (filter active, review completes, delta base
  still resolves); B6 `cache_read_tokens > 0` measurement; B9 one live call
  per strict tool.
- **DoD (CLAUDE.md):** all three service suites + ruff green; `cd tui && go
  build ./... && go vet ./... && go test ./...` green.

## Open questions

- None blocking. Prod-host memory headroom for B7's second replica should be
  confirmed at deploy time (2 × 1G worker limits + existing services).
