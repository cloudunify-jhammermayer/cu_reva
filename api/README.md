# api/ — FastAPI webhook receiver + internal REST API

Two responsibilities in one process:

1. **`POST /webhooks/github`** — the public ingress. Verifies the GitHub HMAC
   signature, stores every delivery, and turns reviewable events into
   `pending_reviews` rows (for the scheduler) or directly enqueued jobs.
2. **`/api/v1/*`** — the internal read/admin API consumed by the Go TUI
  (metrics, reviews, findings, failures, repos, pending, tickets, timesheets, plus admin
   actions like requeue and on-demand audit).

The API does **no LLM work** — it records events and enqueues jobs, keeping the
heavy, retry-prone work isolated in the worker. The one exception: on a
`/review` comment it posts an immediate best-effort "on it" acknowledgement
comment so the developer gets instant feedback while the review is queued.

## Layout

| Path | Role |
|---|---|
| `app/main.py` | Lifespan: load settings, run DB migrations, build the Redis queue + GitHub client onto `app.state`. Mounts routers. |
| `app/settings.py` | Frozen `Settings`; `from_env()`. Fails closed when `REVA_REQUIRE_API_KEY=true` but `REVA_API_KEY` is empty. |
| `app/security.py` | `verify_signature` — constant-time HMAC-SHA256 over the **raw** request body. |
| `app/dependencies.py` | DI providers (`get_db`, `get_settings`, `get_queue`, `get_github_client`, `get_redis`) plus `require_api_key` (fail-closed Bearer-token gate on `/api/v1`). |
| `app/ratelimit.py` | In-memory per-client (API key / IP) rolling-minute cap on `/api/v1`. Off when `REVA_API_RATE_LIMIT_PER_MINUTE=0`; per-instance, so it complements nginx's limit. |
| `app/pagination.py` | `clamp_limit` / `clamp_offset` — bound list-endpoint paging so a huge `offset` can't trigger a deep-offset table scan. |
| `app/routes/webhooks.py` | Signature check → record event → dispatch. Blocking DB work runs in the threadpool so it never stalls the event loop. Idempotency is keyed on a `processed` flag set only after all downstream writes commit, so a mid-handling failure leaves the delivery reprocessable on GitHub's retry instead of silently dropped. PR pushes upsert a debounced `pending_review`; `/review` & `/deep-review` comments trigger immediately (gated to OWNER/MEMBER/COLLABORATOR, bots skipped); inline-comment replies enqueue `run_comment_reply`. |
| `app/routes/health.py` | `GET /health` — checks Postgres **and** the Redis broker; returns `503` (`{"status":"degraded"}`) if either is down so orchestration/the TUI see it. |
| `app/routes/v1/health.py` | `GET /api/v1/health` — credentialed connection test: accepts the master key **or** a per-instance Odoo key and reports which matched (`authenticated_as`, `instance`). For "Test connection" buttons; the root `/health` stays the unauthenticated probe. |
| `app/routes/v1/*` | One router per resource: metrics, reviews, findings, failures, repos, pending, ticket_analyses, ticket_issues, timesheet_reviews, audits, admin. Gated by `require_api_key` + the rate limiter; list endpoints clamp `limit`/`offset`. |
| `app/queries/*` | Read-side SQL (kept out of the route handlers). |
| `app/schemas/*` | Pydantic response models. |

## Auth — why it matters

`/api/v1` exposes operational data and admin actions (requeue, audit), and in
production it's reachable through nginx. `require_api_key` enforces an
`Authorization: Bearer <REVA_API_KEY>` and **fails closed**: with
`REVA_REQUIRE_API_KEY=true` an unset key both refuses startup *and* makes the
dependency reject requests with `503` — it never serves `/api/v1` unauthenticated.
The key is only optional in explicit local-dev mode (`REVA_REQUIRE_API_KEY`
unset/false and no key). `/webhooks/github` is authenticated separately by the
GitHub HMAC signature.

## Audit endpoints

- **`POST /api/v1/repos/{repository_id}/audit`** — enqueue a full repository
  audit. Returns `202` with `{"job_id": "...", "repository_id": N}`. The audit
  runs the whole repo on the deep model (Opus 4.8) against the default branch,
  persists findings, and opens GitHub issues for major/critical findings.
- **`GET /api/v1/audit-findings`** — list audit findings across repos
  (newest/most-severe first). Query params: `severity` (comma-separated, e.g.
  `critical,major`), `repo` (full name like `owner/name`), `limit` (default
  `100`, max `500`). Response: `{"items": [...], "total": N}` where each item
  has `id`, `audit_run_id`, `repo_full_name`, `severity`, `category`, `title`,
  `confidence`, `file_path`, `line_start`, `github_issue_number`, `created_at`.

Both live under the `/api/v1` router, so they share its Bearer auth
(`REVA_API_KEY`) and rate limiting.

## Odoo endpoints

- **`POST /api/v1/timesheet-review`** — instance-key-gated batch intake for
  timesheet wording review. Creates a pending `timesheet_review_runs` row and
  enqueues `worker.timesheet_tasks.run_timesheet_review`; the worker callbacks
  Odoo at `/hr/timesheet-results`.
- **`GET /api/v1/timesheet-reviews`** — master-key list endpoint consumed by the
  TUI Timesheets tab. Returns run metadata and counts only; original line
  descriptions are not stored.

## Tests

```bash
cd api && python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/     # 68
```

`TestClient` + SQLite in-memory (`StaticPool`, `check_same_thread=False` so the
threadpool handlers see the same DB). No network, Redis, or Docker required.
