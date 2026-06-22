# Multiple Odoo instances — per-instance keys, callbacks & cost

**Date:** 2026-06-22
**Component:** `cu_reva` — new `odoo_instances` table + ORM, inbound auth (`api`), ticket/issue routes (`api`), worker callback wiring (`worker`), shared encryption helper (`reva`), new TUI tab (`tui`). No `ast-odoo` / Odoo-contract changes.
**Status:** Design approved, pending implementation plan

## Problem

We will run **multiple Odoo instances**, each sending data to REVA — ticket
analysis today, more features later. The current integration assumes **one**
Odoo:

- **One inbound key.** All `/api/v1` routes are guarded by a single shared
  `REVA_API_KEY`; there is no per-caller identity, so REVA cannot tell which
  Odoo sent a request.
- **One outbound target.** REVA calls back into Odoo (`write-field`,
  `issues-created`, `issue-state`, `reset-status`) using a single
  `ODOO_CALLBACK_URL` + `ODOO_CALLBACK_API_KEY`, hardcoded at deploy time.
- **No source scoping.** `ticket_analyses` / `ticket_issue_runs` are keyed by
  `(ticket_id, model_name, …)` with no instance column — the same `ticket_id`
  from two Odoo instances would collide, and spend cannot be attributed to a
  source instance.

We want each Odoo instance to be a first-class, **TUI-creatable** record with
its own REVA-minted API key and its own callback config, and we want the TUI to
show **tokens / cost per instance**.

## Context

Current flow (mapped against the code, 2026-06-22):

- **Inbound auth:** `api/app/dependencies.py::require_api_key` does a single
  `hmac.compare_digest(auth, f"Bearer {settings.api_key}")` (constant-time)
  against `REVA_API_KEY`. Applied to all `/api/v1` routes. The TUI uses the same
  key.
- **Odoo-originated routes:** `POST /api/v1/ticket-analysis` (+ GET, requeue) in
  `api/app/routes/v1/ticket_analyses.py`; `POST /api/v1/create-issues` (+ list,
  GET, requeue) in `api/app/routes/v1/ticket_issues.py`.
- **Outbound callbacks:** `reva/odoo_client.py::OdooCallbackClient(callback_url,
  api_key)` — built once in the worker context (`ctx.odoo`) from env settings
  (`worker/worker/runner.py`). Constructor already treats an **empty
  `callback_url` as "disabled"** and derives sibling endpoints from a
  `/write-field` suffix; methods raise `PermanentError` when disabled.
- **Data + cost:** `ticket_analyses` and `ticket_issue_runs` each carry
  `input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
  estimated_cost_usd`. `ticket_issue_runs` has a partial unique index
  `idx_ticket_issue_runs_pending ON (ticket_id, model_name) WHERE
  status='pending'`. `ticket_analyses` dedups `(ticket_id, model_name,
  field_name)` via a SELECT in the route handler (not a constraint).
- **Spend ledger:** every paid ticket call also writes a `claude_spend` row
  (`kind='reply'`) used solely for the rolling 24h budget cap.
- **Deployment reality:** the Odoo ticket-analysis / create-issues features are
  **not yet deployed** — only PR reviews run in production today. There is no
  live single-Odoo deployment to remain backward-compatible with.
- **Multi-repo precedent:** repositories are scoped by a `repository_id` FK and
  identified by a natural `full_name`; `POST /api/v1/repos` is the existing
  "create a resource" route pattern. The TUI is otherwise read-only.
- **Secrets convention:** `reva/config.py::env_or_file(NAME)` resolves
  `NAME` or `NAME_FILE`. `cryptography==48.0.0` is already a direct dependency
  (pulled in for GitHub-App RS256 JWTs), so **Fernet is available with no new
  dependency**.

### Locked decisions

1. **Key = identity, Odoo-scoped.** REVA mints a unique inbound key per
   instance; the key alone identifies the instance (no `instance_id` in the
   payload). Instance-scoped keys may reach only the Odoo/ticket create routes;
   the existing `REVA_API_KEY` remains the admin/TUI **master** key with full
   access.
2. **Per-instance outbound callbacks.** Each instance record stores its own
   `callback_url` + outbound `callback_api_key`.
3. **Approach 2 — scoped tenant, cost from the run tables.** Add `odoo_instances`
   + an `odoo_instance_id` FK on the **two ticket tables only**. `claude_spend`
   is left untouched (it stays the pure global budget mechanism). Per-instance
   cost is summed directly off the run tables.
4. **No legacy single-Odoo path.** Because the ticket features are not deployed,
   there is nothing to stay compatible with: every Odoo request must carry an
   instance key, `odoo_instance_id` is required on new ticket runs, and the old
   `ODOO_CALLBACK_URL` / `ODOO_CALLBACK_API_KEY` env path is **removed** rather
   than kept as a fallback.
5. **Cost view:** per instance — lifetime + rolling 24h + rolling 30d, each
   **split by task type** (ticket analysis vs create-issues).
6. **Outbound key encrypted at rest** with Fernet, keyed by a new
   `REVA_SECRET_KEY` (`env_or_file`) — adopted directly, not deferred behind a
   plaintext interim.

### Explicitly out of scope

- No per-instance budget caps (the global `REVA_DAILY_BUDGET_USD` cap stays as
  the only cost control).
- No change to the Odoo-side contract or to `OdooCallbackClient`'s request
  payloads — only *which* URL/key the client is constructed with changes.
- No change to the `claude_spend` ledger or the budget advisory-lock logic.

## Design

### 1. Data model

New migration (next number after `012`) + ORM model `OdooInstance` in
`reva/db/models.py`. Idempotent SQL (`CREATE TABLE IF NOT EXISTS`, `id
BIGSERIAL PRIMARY KEY`, matching existing file conventions).

`odoo_instances`:

| column | type | notes |
|---|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` | internal FK target |
| `name` | `TEXT NOT NULL UNIQUE` | human label, e.g. `"ACME Production"` — natural id in TUI/cost views |
| `key_hash` | `TEXT NOT NULL UNIQUE` | SHA-256 hex of the minted inbound key (never the plaintext) |
| `key_prefix` | `TEXT NOT NULL` | first ~12 chars (e.g. `odoo_3f2a9c…`), non-secret, for display + rotation UX |
| `callback_url` | `TEXT NOT NULL DEFAULT ''` | outbound write-field base; `''` = callbacks disabled (matches `OdooCallbackClient`) |
| `callback_api_key_enc` | `TEXT NOT NULL DEFAULT ''` | Fernet-encrypted outbound Bearer (`''` = none) |
| `active` | `BOOLEAN NOT NULL DEFAULT true` | deactivate = soft delete; keeps cost history + FK rows intact |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `updated_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |

Add to **both** `ticket_analyses` and `ticket_issue_runs` (idempotent `ADD
COLUMN IF NOT EXISTS`):

```
odoo_instance_id  BIGINT REFERENCES odoo_instances(id)
```

The column is **nullable at the DB level** (Postgres can't add a `NOT NULL`
column without a default to a possibly-non-empty table, and migrations must stay
idempotent), but **required by the application layer** — the create routes
always stamp the resolved instance, so every real row has it set. Matching FK
fields on the `TicketAnalysis` / `TicketIssueRun` ORM models, plus the
`OdooInstance` model (tests build from the models).

**Dedup constraint upgrade** (the only index change). Replace
`idx_ticket_issue_runs_pending` with:

```sql
CREATE UNIQUE INDEX idx_ticket_issue_runs_pending
    ON ticket_issue_runs (odoo_instance_id, ticket_id, model_name)
    WHERE status = 'pending';
```

This dedups one in-flight run **per instance** per `(ticket_id, model_name)`,
fixing the cross-instance `ticket_id` collision (instance A and instance B may
each have a pending run for ticket 42; the same instance may not). The
`ticket_analyses` route dedup SELECT gains `odoo_instance_id` in its filter.

> The ORM `Index` carries `sqlite_where`, so the partial unique constraint **is**
> enforced on SQLite and the cross-instance dedup is unit-testable. The raw
> migration SQL (the `DROP`/`CREATE INDEX` file) only runs on Postgres, so
> validate the migration itself via `make test-integration` / first staging boot.

### 2. Encryption helper (`reva`)

New small module (e.g. `reva/secrets_crypto.py`): a Fernet wrapper keyed by
`REVA_SECRET_KEY` (`env_or_file`), exposing `encrypt(plaintext) -> str` /
`decrypt(token) -> str`. Used by the API to seal `callback_api_key_enc` on
write and by the worker to open it when constructing the callback client.

- `REVA_SECRET_KEY` must be a valid Fernet key (URL-safe base64, 32 bytes). A
  helper/CLI note documents `Fernet.generate_key()` for ops. The feature cannot
  operate without it (outbound callbacks are per-instance and sealed).
- If `REVA_SECRET_KEY` is unset: creating/editing an instance **with** a
  non-empty outbound key fails fast (clear 400); an instance with an empty
  outbound key works (callbacks disabled). On the worker, `build_odoo_client`
  decrypts the sealed key eagerly at the start of the job; if `REVA_SECRET_KEY`
  is absent (or the instance row is gone) it raises before the job's
  try-block, so RQ marks the job failed and the run row is left for the
  stale-`running` reaper / a manual requeue rather than being marked failed
  with an Odoo failure-callback. (As-built limitation — not a silent no-op: the
  job fails loudly. A missing-key build failure also can't notify Odoo, since
  sending the failure callback would itself need a client. Deployments wire
  `REVA_SECRET_KEY` via compose, so this is the degenerate-misconfig path.)

### 3. Inbound auth + scoping (`api`)

Extend `api/app/dependencies.py` without breaking the master path:

- `Authorization: Bearer <token>`:
  1. If `compare_digest(token, settings.api_key)` → **admin** caller (full
     access). Unchanged for the TUI.
  2. Else SHA-256 the token and look up `odoo_instances` by `key_hash WHERE
     active` → if found, caller is **that instance**.
  3. Else `401`.

A new dependency `require_odoo_instance` resolves a concrete **active**
`OdooInstance` (or `401`). The two **create** routes (`POST /ticket-analysis`,
`POST /create-issues`) switch from `require_api_key` to `require_odoo_instance`,
reject the master key, and **stamp the resolved `odoo_instance_id`** onto the run
row they create. The **read + requeue** routes (`GET …`, `…/requeue`) keep
master-only `require_api_key` — they operate on an existing row whose
`odoo_instance_id` is already persisted (admin/TUI operation). All
instance-management routes are master-only. Instance-scoped keys therefore reach
only the two create routes.

The hashed-key lookup is an indexed exact match on `key_hash`; the master key
keeps its constant-time compare. (Per-instance keys are high-entropy random
tokens; an attacker cannot enumerate them, so the negligible timing of a hashed
indexed lookup is acceptable.)

### 4. Outbound callbacks + worker wiring (`worker`)

`OdooCallbackClient` itself is unchanged. The worker currently builds it once
from env settings and stashes it on the job context (`ctx.odoo`). Replace that
with **per-job** construction: each ticket/issue runner loads its run row's
`OdooInstance`, decrypts `callback_api_key_enc`, and builds
`OdooCallbackClient(instance.callback_url, decrypted_key)`. The env-based
construction and `ctx.odoo` are removed, along with the now-orphaned
`ODOO_CALLBACK_URL` / `ODOO_CALLBACK_API_KEY` settings on the api + worker
`Settings`. An empty per-instance `callback_url` still yields a disabled client
(existing `OdooCallbackClient` behavior) — methods raise `PermanentError` rather
than silently no-op.

### 5. API endpoints (`api`)

New **admin-only** CRUD under `/api/v1/odoo-instances`, following the
`POST /api/v1/repos` precedent (Pydantic schemas in
`api/app/schemas/odoo_instances.py`, queries in
`api/app/queries/odoo_instances.py`, writers in `reva/db/writers.py`):

- `POST /api/v1/odoo-instances` — body `{name, callback_url, callback_api_key}`.
  Mints the inbound key, persists `key_hash`/`key_prefix` + Fernet-sealed
  outbound key. **`201` returns the plaintext inbound key exactly once** plus
  the record (key_prefix only thereafter).
- `GET /api/v1/odoo-instances` — list records (never the secret) with the cost
  summary folded in (§6).
- `GET /api/v1/odoo-instances/{id}/cost` — per-instance cost detail (§6).
- `POST /api/v1/odoo-instances/{id}/rotate-key` — mint a new inbound key
  (new hash/prefix; old key invalid immediately), return plaintext once.
- `PATCH /api/v1/odoo-instances/{id}` — update `name` / `callback_url` /
  `callback_api_key` (re-sealed) / `active`.

The plaintext inbound key and the outbound key are **never** returned by any GET.

### 6. Cost aggregation (`api`)

Computed straight off the run tables — no `claude_spend` involvement. For each
instance, over each window (lifetime, last 24h, last 30d) and grouped by task
type:

- `ticket_analyses` → task type **analysis**: `SUM(estimated_cost_usd)`,
  `SUM(input_tokens)`, `SUM(output_tokens)`, count.
- `ticket_issue_runs` → task type **create-issues**: same aggregates.

Filtered by `odoo_instance_id` and `created_at` window. Exposed both as a
compact summary on the list endpoint and in full on the per-instance cost
endpoint. (Failed runs contribute whatever cost was recorded before failure —
we sum what's persisted.)

### 7. TUI (`tui`)

New **"Odoo"** tab — the TUI's first write path — following the existing tab +
`internal/api/{client,iface,mock,types}.go` patterns:

- **List:** name, key_prefix, callback host, active flag, and cost columns
  (lifetime / 24h / 30d USD, split analysis vs create-issues).
- **Create:** prompt name + callback URL + outbound key → POST → **display the
  minted inbound key once** with a clear "copy now — it won't be shown again".
- **Actions:** rotate key (shows new key once), toggle active, edit callback
  config.

`cd tui && go build ./... && go vet ./... && go test ./...` must stay green.
Add the corresponding methods to `iface.go` + a `mock.go` implementation.

### 8. Migration, setup, rollout

- One idempotent migration: create `odoo_instances`, add the `odoo_instance_id`
  FK column to both ticket tables, and replace the pending unique index with the
  `(odoo_instance_id, ticket_id, model_name)` variant. Safe because the ticket
  features are not deployed (no rows to backfill).
- Add `REVA_SECRET_KEY` (`env_or_file`) to the api + worker settings; **remove**
  `ODOO_CALLBACK_URL` / `ODOO_CALLBACK_API_KEY`. Document generating the Fernet
  key.
- Setup (no cutover needed — nothing live): deploy → in the TUI create each Odoo
  instance (name + callback URL + outbound key) → configure each Odoo to send
  its minted inbound key. PR reviews are unaffected throughout (they never
  touched the Odoo path).

## Testing

Unit (SQLite + mocks):

- Key minting → hash stored, plaintext returned once; `key_prefix` derived.
- Auth: instance key resolves to its instance and can create runs; instance key
  **rejected** on management routes; master key **rejected** on the two create
  routes but accepted on management/read/requeue; bad/inactive key `401`.
- Resolved `odoo_instance_id` is stamped on created `ticket_analyses` /
  `ticket_issue_runs` rows.
- Worker builds the callback client from the run's instance (decrypted key);
  empty instance `callback_url` → disabled client raises `PermanentError`.
- Fernet round-trip; missing `REVA_SECRET_KEY` → create-with-key fails fast,
  worker-with-sealed-key raises `PermanentError`.
- Cost aggregation: lifetime/24h/30d windows and analysis-vs-issues split return
  correct sums for a fixture spanning two instances.

Unit (SQLite enforces the partial unique index via `sqlite_where`):

- The `(odoo_instance_id, ticket_id, model_name)` partial unique index: two
  instances may each have a pending run for the same `(ticket_id, model_name)`;
  a second pending run for the **same** instance + ticket is rejected.

Integration (`make test-integration`, real Postgres):

- The raw `018` migration applies cleanly (table + FK columns + index swap) on a
  schema migrated from the previous version.

Run `worker`, `api`, **and** `scheduler` suites (shared `reva/` change) + `ruff`
+ the Go TUI build/vet/test.

## Open questions

None outstanding — all six locked decisions above are resolved.
