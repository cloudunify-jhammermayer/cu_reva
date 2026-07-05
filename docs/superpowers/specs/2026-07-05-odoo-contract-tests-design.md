# Odoo↔REVA contract tests — design

**Date:** 2026-07-05
**Component:** `cu_reva` — new `reva/odoo_contracts.py` (payload models + generator), generated `contracts/` dir (committed), drift tests, `scripts/sync_contracts.sh`; plus a documented consumption contract for the `ast-odoo` addon repo (tasks executed there).
**Status:** Design approved (Q&A with Joseph, 2026-07-05), pending implementation plan.

## Problem

The Odoo↔REVA integration contracts live as prose: `reva/odoo_client.py`
docstrings, spec documents, and comments like `CreateIssuesRequest`'s "field
set fixed by the shipped Odoo addon — do not add required fields". Drift is
caught in production: the 2026-07-05 Odoo-side move to namespaced callbacks
(`/write-field` → `/tickets/write-field`) surfaced as a breaking change, not
a failing test. Three more contracts are queued (`/hr/` timesheets,
`/metasoul/` website analysis, per-instance quota 429s).

## Context (verified 2026-07-05)

- Outbound: `OdooCallbackClient` builds payload dicts inline for
  `/tickets/write-field`, `/tickets/reset-status`, `/tickets/issues-created`,
  `/tickets/issue-state`; the URL namespace exists only in code + docstring.
- Inbound: `TicketAnalysisRequest` and `CreateIssuesRequest` are Pydantic
  models (FastAPI-validated); `GET /api/v1/health` returns
  `{status, authenticated_as, instance}`; errors are `{detail: str}` (401,
  quota 429 from the hardening batch, 409 requeue conflicts).
- The repo already has the "drift test" pattern (`.env.example` coverage
  test from the hardening batch).
- `ast-odoo` is a separate repo (Odoo addons) with its own test setup; the
  typed-issue-requests plan already used the "addendum tasks executed in
  ast-odoo" pattern.

### Locked decisions

1. **Pydantic-first:** callback payloads become Pydantic models that
   `OdooCallbackClient` uses **at runtime** (`.model_dump()`), so code and
   contract cannot diverge by construction. (Rejected: hand-written
   contract-first schemas — permanent two-way drift checking; full OpenAPI —
   outbound callbacks aren't FastAPI.)
2. **Vendored consumption:** REVA generates `contracts/` (schemas + samples +
   manifest); `scripts/sync_contracts.sh` copies it into ast-odoo where it is
   committed; addon tests validate against the vendored copy (offline-safe
   for Odoo.sh CI). A manifest version hash catches stale vendoring.
3. **The manifest carries URL paths, methods, auth, and direction** — a path
   rename fails tests on both sides.
4. **v1 scope = everything live today:** 4 ticket callbacks, 2 inbound create
   requests, the health response, error body shapes. Future contracts
   (`/hr/`, `/metasoul/`, …) join at their implementation time — enforced by
   a coverage drift test (every public `OdooCallbackClient` method must have
   a manifest entry), and the pending plans reference this spec.
5. REVA-side sample validation uses Pydantic round-trips (no new
   dependency); the ast-odoo side may use `jsonschema` (already in Odoo's
   dependency set).

### Explicitly out of scope

- Runtime request/response validation in production (contract *tests* only;
  FastAPI already validates inbound at runtime).
- Publishing contracts over HTTP (`GET /api/v1/contracts`) — revisit if a
  third consumer appears.
- GitHub webhook contracts (GitHub owns those), Google Chat payloads.

## Design

### 1. Contract artifacts — `contracts/` (generated, committed in REVA)

```
contracts/
  manifest.json
  README.md                     # what this is, how to regenerate, how to sync
  callbacks/
    tickets.write-field.schema.json      tickets.write-field.sample.json
    tickets.reset-status.schema.json     …
    tickets.issues-created.schema.json   …
    tickets.issue-state.schema.json      …
  inbound/
    ticket-analysis.schema.json          ticket-analysis.sample.json
    create-issues.schema.json            …
    health.response.schema.json          health.response.sample.json
    error.schema.json                    # {detail: str} — 401/409/429 bodies
```

`manifest.json`:

```json
{
  "contracts_version": "<sha256 over all schema+sample bytes>",
  "generated_from": "reva/odoo_contracts.py",
  "contracts": [
    {"name": "tickets.write-field", "direction": "reva->odoo",
     "method": "POST", "path": "/tickets/write-field",
     "auth": "bearer:instance-outbound-key",
     "schema": "callbacks/tickets.write-field.schema.json",
     "sample": "callbacks/tickets.write-field.sample.json"},
    {"name": "ticket-analysis", "direction": "odoo->reva",
     "method": "POST", "path": "/api/v1/ticket-analysis",
     "auth": "bearer:instance-inbound-key", "...": "..."}
  ]
}
```

### 2. REVA side — `reva/odoo_contracts.py`

- Payload models: `WriteFieldPayload`, `ResetStatusPayload`,
  `IssuesCreatedPayload` (with `IssueRefPayload` items),
  `IssueStatePayload` — field-for-field what `OdooCallbackClient` sends
  today (byte-identical JSON; a regression test asserts it).
- `OdooCallbackClient` methods construct their bodies through these models.
- A declarative `CONTRACTS` table in the same module maps model → name/
  direction/method/path/auth (single source for the manifest; paths written
  once, next to the models).
- Golden samples are literal dicts in the module, validated by a
  `model_validate` round-trip test (a sample that stops validating fails CI).
- Generator CLI: `python -m reva.odoo_contracts generate [--check]` —
  `generate` writes `contracts/`; `--check` regenerates to a temp dir and
  diffs against the committed copy (the drift test runs this).

### 3. Drift + coverage tests (worker suite)

1. Regenerated output == committed `contracts/` (byte-for-byte).
2. Every public `OdooCallbackClient` method (introspected) has a `CONTRACTS`
   entry — the timesheet/metasoul plans cannot add `/hr/`/`/metasoul/`
   callbacks without publishing their contracts.
3. Every sample validates against its model; `IssuesCreatedPayload` sample
   covers both `status: created` and `status: failed` shapes.
4. Inbound schemas equal the FastAPI models' `model_json_schema()` (they are
   generated from the same classes, so this guards accidental decoupling).

### 4. Sync — `scripts/sync_contracts.sh <ast-odoo-path>`

Copies `contracts/` → `<ast-odoo>/reva_contracts/` (rsync --delete), prints
the `contracts_version`, reminds to commit there. No network, no coupling.

### 5. ast-odoo side (consumption contract; tasks executed in that repo)

Documented as an addendum in the implementation plan:
- Vendored `reva_contracts/` committed.
- Per inbound-controller test: the vendored **sample** is accepted (2xx),
  a mutated sample (wrong types/missing required) is rejected, and the
  controller's route equals the manifest `path`.
- Per outbound-request test: the addon's request bodies validate against the
  vendored inbound schemas (`jsonschema`).
- A freshness test pins the expected `contracts_version` so "synced but not
  reviewed" drift is a conscious, visible bump.

## Error handling

| Case | Behavior |
|---|---|
| Generator output differs from committed `contracts/` | worker suite fails (`--check`) with the diff — regenerate + commit |
| A callback method without a `CONTRACTS` entry | coverage test fails naming the method |
| Sample no longer validates | test failure naming the sample |
| ast-odoo vendored copy stale | its freshness test fails on the version pin |

## Testing

All REVA-side tests are pure/offline (Pydantic round-trips, file diffs,
introspection) — no Docker, no network, no paid calls. The ast-odoo tests run
in that repo's Odoo test harness. First real payoff check: re-apply the
`/tickets/` rename scenario mentally — the manifest path change would have
failed REVA's drift test and ast-odoo's route test before deploy.

## Open questions

- None blocking. The `/hr/` and `/metasoul/` contracts are added by their
  own plans (both already carry a pointer to this spec after it lands).
