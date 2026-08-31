# REVA — Work Handoff

## Addendum 2026-08-31 — docs site renders `.html` docs

**Status: implemented** (spec
`docs/superpowers/specs/archive/2026-08-31-docs-site-html-rendering-design.md`,
plan `docs/superpowers/plans/archive/2026-08-31-docs-site-html-rendering.md`,
both archived on completion). `reva/repo_docs.py::browser_in_scope` widens the
docs browser to `.html`/`.htm` inside a `docs/` folder — the repo root's or an
addon's own. `in_scope` is unchanged, so the ticket-analysis grounding index
still sees markdown only: no `_SCOPE_VERSION` bump, no re-index, no migration.

The SPA renders HTML through the same DOMPurify-sanitized pipeline as markdown
(`renderHtml` in `docs-ui/src/markdown.js`), with `<style>` stripped so a doc
cannot restyle the site. `.html` is deliberately still refused by `/raw` — it is
never served as a document from our origin.

**Deploy:** nginx must be rebuilt for the SPA change
(`docker compose -f docker-compose.prod.yml build nginx && … up -d nginx`),
plus the usual api redeploy for the `/repo-docs` change. Frontend behavior is
NOT yet verified in a browser — the plan's Task 3 Step 4 check is owed.

## Addendum 2026-08-20 — the Odoo side's three requests

Source: `Cloudunify/custom_addons/cu_reva_ticket_analysis/docs/reva-side-requests.md`
(module 19.0.39.0.0). The Odoo half of all three is already shipped and degrades
until REVA catches up.

**Requests 2 and 3: DONE** (commit `2a6d6bc`, not yet deployed).

- *Support run status* turned out to be ~90% built: `GET
  /api/v1/support-turn/{turn_id}` already had exactly the auth and 404 semantics
  the handoff asked for. Only `confidence` and `sources_html` were missing, and
  they are now derived server-side from `result_structured`. **No new route** —
  the Odoo side should call `/support-turn/{turn_id}`, and map `answer_html` /
  `error_message` itself (`/ticket-analysis/{id}` returns `result_html` /
  `error_message`, so the handoff's proposed `html`/`error` names match neither
  endpoint).
- *Turn id on the callback* was half done: the `202` already returned `turn_id`
  and Odoo already stored it. The gap was the callback, which now carries
  `turn_id`. **`analysis_id` was added at the same time**: Odoo's write-field
  staleness guard has always enforced a non-zero `analysis_id`, but REVA never
  sent one, so that guard was dead code and the analysis path had the identical
  race.
- **Contracts synced 2026-08-20** into `Cloudunify/reva_contracts/`, pin bumped
  to `8f7c2d31d57f…`, 834 Odoo tests green. The new `analysis_id: 456` in
  `tickets.write-field.sample.json` correctly 409'd an Odoo fixture — the
  staleness guard firing for the first time; the fixture's `reva_analysis_id`
  was set to match rather than the field stripped. **REVA is DEPLOYED to prod
  as of 2026-08-21 (585b5b7); the Odoo side is not.**
- **Odoo still owes the support half of the guard:** REVA sends `turn_id`, but
  `WriteFieldRequest` does not declare it, so Pydantic drops it. Accept it and
  compare against `reva_support_turn_id`. Confirmed for them: the `202` from
  `POST /api/v1/support-request` returns `turn_id` (`SupportRequestCreated`) and
  Odoo already stores it in both submit paths, so the stored id is never 0.

**Request 1 (issue reassignment): IMPLEMENTED and DEPLOYED 2026-08-21** (commits
`f80b923`..`c7f2efc`, merged as `585b5b7`). Spec:
`docs/superpowers/specs/archive/2026-08-20-issue-reassignment-design.md`. An
override table (`ticket_issue_reassignments`, migration 047) honored by five
owner-resolution sites; `POST /api/v1/reassign-issue` deliberately never
returns 404, because Odoo's wizard reads that as "not shipped yet" and commits
the move with a warning that would be false.

- **Owed: the contract re-sync.** Run `scripts/sync_contracts.sh <path to
  Cloudunify/>` to land the new `reassign-issue` contract in
  `Cloudunify/reva_contracts/`, then bump the `contracts_version` pin in
  `cu_reva_connector/tests/test_contracts.py` to
  `6b7ab42efc3b215ee037bca2c954f9fd506ac557795e10b87d394dd3004e7577`
  (supersedes `8f7c2d31d57f…`, synced 2026-08-20) — the value
  `python -m reva.odoo_contracts generate` printed. **REVA is deployed; the
  Odoo side is not, which is the safe order — the endpoint must exist before
  the wizard calls it, and the wizard tolerates its absence either way.**
- **Deploy verification (2026-08-21).** `scripts/deploy.sh` run detached so an
  interrupted session could not leave a half-restart; exit 0, all services
  healthy. Migration 047 recorded at version 47 on prod Postgres with both
  indexes and the FK present. `POST /api/v1/reassign-issue` answers **401**
  unauthenticated — it exists and is instance-gated, and specifically is not a
  404. No ops events in the 20 minutes after the deploy. Prod's uncommitted
  `docker-compose.prod.yml` drift (value-report cadence env passthrough) was
  stashed before the pull and popped after; it is still uncommitted there.
- **Coverage before deploy.** Unit: scheduler 37, api 360, worker 1598, ruff
  clean, TUI green. Integration: `make test-integration` 16 tests on real
  Postgres 16, whose fixture calls `db.migrate()` — so migration 047's raw SQL
  DID execute against real Postgres, not only the ORM. Plus a throwaway
  pre-deploy smoke exercising every new query path (both union directions,
  cross-instance isolation, the notify redirect, the `or_(and_(...))` estimate
  filter, the four-column `.distinct()`) on Postgres 16.
- **Still not covered:** no live Odoo call was made, and no request from the
  real Odoo wizard has ever been received. Migration
  `047`'s raw SQL is not exercised by the unit tier — those build tables from the
  ORM models via `create_all`, not from the SQL files — so it is validated
  only by `make test-integration` against real Postgres, or by the first
  staging boot. Two SQL constructs this work added — an `or_(and_(...))`
  predicate and a four-column `.distinct()` — have run only against SQLite,
  never Postgres. Verified at `1bdade3`: worker 1597 passed / 15 skipped, api
  360 passed, scheduler 37 passed / 1 skipped, ruff clean.
- **Design consequence worth knowing before go-live:** for a moved issue, the
  merged-PR change note is generated from the *source* ticket's name but
  delivered to the *target* record, because
  `worker/worker/change_note_runner.py` reads the ticket name off the run
  that holds the issue plan. This is a deliberate accepted imperfection
  recorded in the spec, not a bug — but the note text is customer-facing, so
  whoever deploys this should know.

**One thing the handoff gets wrong, worth knowing before reading it:** it
attributes mis-filed issues to REVA parsing `cr/1234` from the branch or PR
title. Branch parsing only feeds the ticket-level PR work-status signal, which
never creates an issue row. Issue placement comes from the create-issues request
Odoo itself addressed at a record. The remedy is unchanged, but the reassignment
endpoint will **not** fix a mistyped branch.

## Addendum 2026-08-12 — support answers can read the customer's screenshots

**Status: REVA side implemented and deployed. The Odoo side is NOT built —
that is tomorrow's work, and until it ships this feature does nothing.**

- Spec: `docs/superpowers/specs/2026-08-10-support-answer-images-design.md`
- REVA plan (done): `docs/superpowers/plans/2026-08-10-support-answer-images.md`
- **Odoo handoff (start here): `docs/superpowers/plans/2026-08-12-support-answer-images-odoo.md`**

**What prompted it.** Ticket 6891 arrived with two screenshots that *were* the
question — a kit BOM showing 1.000,00 L of glycol inside a 1 Stück kit. REVA got
only html2plaintext's `Image [1]` placeholders plus `/web/image/…` footnote URLs
it cannot fetch, answered `partially_answered`, and asked which product was
affected — which the first screenshot showed.

**What shipped.** `POST /api/v1/support-request` takes `images: [{filename,
label, content_base64}]`; `reva/image_attachment.py` gates them (extension
decides the type, magic bytes verify it; 6 images / 5 MB each / 8 MB total);
`ClaudeClient.review(images=…)` emits image content blocks laid out
preamble → labelled images → prompt. The CLI escalation stages images as files
via `--add-dir` for the already-allowed `Read` tool.

**Two things to know.**

1. **`ast-odoo is retired as of 2026-08-12.`** The Odoo half goes in
   `../Cloudunify` only. Contracts (`contracts_version 3421e338…`) still need
   copying into `Cloudunify/reva_contracts/` — that is step 0 of the Odoo plan.
2. **Requeue drops images** (it rebuilds params from the DB, like `chatter` and
   `attachment`). `support_turns.image_count` (migration 046) plus a
   `requeue_lost_images` ops event and a TUI Img column make that visible. The
   operator fix is re-pressing the Odoo button, not requeue.

**Coverage, stated honestly.** `make test` green (scheduler 37, api 344, worker
1566), ruff and the Go TUI clean. But every test mocks the Messages API and the
Claude CLI, so green proves the *request shape*, not that the model reads a
screenshot. Migration 046 is a single idempotent `ADD COLUMN IF NOT EXISTS`
applied at startup; no Docker was available locally, so it was not exercised by
`make test-integration` before deploy. The end-to-end check — re-send 6891 and
confirm the answer names `[200028]` — is still outstanding and needs the Odoo
side first.

## Addendum 2026-08-07 — docs site shows the repo-root `docs/` folder

**Status: implemented** (spec
`docs/superpowers/specs/archive/2026-08-07-docs-site-root-docs-design.md`, plan
`docs/superpowers/plans/archive/2026-08-07-docs-site-root-docs.md`, both
archived on completion). `reva/repo_docs.py::in_scope` now covers each repo's
top-level `docs/` folder in addition to `custom_addons/`; any `superpowers/`
folder is excluded as a directory segment anywhere in the path.

**Deploy precondition: the Cloudflare Access app — SATISFIED, verified
2026-08-07 before deploying.** The branch raises the stakes on this gate: the
browsable (and groundable) surface used to be addon documentation only, and is
now every registered repo's whole root `docs/` tree, which can hold internal
runbooks. Checked at deploy time — unauthenticated hits on `/docs/` and
`/repo-docs/repos` both `302` to the Access login, so the gate is real (the
runbook's item 4, which claimed the app was never created, was stale and is
now marked done). Also checked: `cu_reva` is **not** a registered repo on
prod, so this repo's own `docs/` — including that runbook and its prod SSH
host — is not published by the browser. **Decided by Joseph 2026-08-07:
`cu_reva` must NOT become an enabled repo in the docs browser** — REVA's own
docs stay non-public. Registering this repo (e.g. to have REVA review its own
PRs) would immediately publish 28 of our own files, including that runbook's
prod SSH host, to everyone past Access. Nothing enforces this in code today;
it holds only because the repo is unregistered.

**Two things to know before deploying.** The scope is shared with
ticket-analysis / support-answer grounding, so root docs now enter
`repo_doc_sections` and can be cited in customer-facing answers — the first
analysis per repo after deploy pays one re-index, forced by
`repo_docs_sync.scope_version` (migration 045) because a scope change does not
move the tree SHA that sync staleness keys on. And `docs-ui` is built into the
nginx image, so the SPA change needs
`docker compose -f docker-compose.prod.yml build nginx` on top of the api/worker
redeploy that carries the migration.

**Rollback edge on `scope_version`.** Old (pre-this-branch) code never writes
the column. Deploy → rollback → a repo's tree moves under the old code → the
old code re-indexes that repo under the old scope while `scope_version` stays
at 1 → rolling forward again then sees a matching stamp and skips the
re-index, leaving that repo's root docs missing from the index until its tree
moves again. Narrow window, self-healing on the next push; no code change made
for it.

**Not live-validated.** Unit-tested (worker + api) and migration-checked on
real Postgres via `make test-integration`; the docs-ui tree change was
verified with a Node one-liner against `buildDocTree` and a clean `npm run
build`, not a manual browser check — that needs the full Docker stack plus a
live GitHub App token. No prod or staging run yet.

## Addendum 2026-08-03/05 — golden estimates (Plan A shipped, Plan B open)

**Status: Plan A implemented and merged to main** (spec
`docs/superpowers/specs/archive/2026-08-04-golden-estimates-design.md`, plan
`docs/superpowers/plans/archive/2026-08-04-golden-estimates.md`, both archived
on completion). Plan B — the calibration view — is still open, blocked on the
same empty `ticket_actuals` table noted below; its spec stays at
`docs/superpowers/specs/2026-07-15-estimate-drift-stats-design.md`.

**Storage decision reversed during design.** This addendum originally locked a
DB table + `/api/v1` + TUI CRUD for anchor management (see the decisions table
below, kept for history). The spec superseded that: `prompts/golden_estimates.yml`
is a checked-in, hand-authored file instead — REVA never writes to it — which
made the whole feature **migration-free** (zero new tables, zero new columns;
`anchor_ref`/`complexity_drivers`/`anchor_confidence` persist into the existing
`ticket_analyses.result_structured` / `ticket_issue_runs.issues` JSON columns).
The "TUI CRUD" and "promote a past analysis" rows below did not ship as a
result — see the spec's "Reversed decisions" section for the full reasoning.

**Anchors written and deployed 2026-08-05 (prompts v2.18).** The file no longer
ships empty: **15 anchors / 30 stories** from real closed AST and BLP tickets,
with hours supplied by Joseph from booked time. Three consequences worth
knowing before touching this:

- **The bands were recalibrated down** (config 0.5–2 → 0.5–1.5, small 1–4 → 1–2,
  medium 3–8 → 2–4, large 6–12 → 4–8), because the actuals showed the old ones
  overshooting: 120–238 h estimated against 65.5 h booked across the 15 tickets,
  and every `cross_module_workflow` story — the band's own "large, 6–12 h"
  example — came in at 2 h. `DEFAULT_BANDS` moves in lockstep with the file;
  `test_shipped_file_matches_the_bands_in_code` pins the two together.
- **Anchor hours and bands are booked time with no margin in them.** A **10 %
  quoting buffer** is applied by the prompt, once, as the last step. Do not pad
  the file — it would stop being a record of what actually happened.
- **30 stories against a render limit of 30.** Zero headroom: the next story
  added silently drops off the end (with an `anchor_limit_exceeded` ops event).
  `REVA_GOLDEN_ESTIMATE_LIMIT` raises it but is **not** wired into
  `docker-compose.prod.yml`, so today that needs a compose edit — as do the
  sibling switches `REVA_GOLDEN_ESTIMATES`, `REVA_TICKET_CODE_GROUNDING` and
  `REVA_CROSS_BRANCH_REUSE`.

**Still not validated live.** Every test is a unit test: no live Claude CLI run,
no real Odoo. The highest-value first check is one ticket analysis that escalates
to the CLI path, confirming the model cites a real `anchor_ref`, the derived
`anchor_confidence` looks sane, the 10 % buffer actually lands in the output,
and no anchor text reaches the Odoo HTML.

### What it is

REVA estimates in two places — ticket analysis (`prompts/ticket_analysis.md` §5,
`StoryEstimate` in `reva/types.py:459`, hour range per story) and the issue
planner (`prompts/ticket_issues.md`, one `estimate_hours` per issue). Both carry
their **own copy** of the same four calibration bands (config 0.5–2 h, small
1–4 h, medium 3–8 h, large 6–12 h) plus one anonymous reference example.

The feature: an operator-curated set of **golden estimates** — real closed
tickets with a written scope and trusted hours — injected into both prompts as
reference anchors, so estimates are anchored on precedent instead of on frozen
prose, and traceable to the anchor they came from.

### Decisions locked 2026-08-03

| Question | Decision |
|---|---|
| Anchor source | Operator-curated from closed tickets — **not** auto-derived |
| Storage / management | DB table + `/api/v1` + **TUI CRUD** (not a checked-in file) — **reversed during design, see above** |
| Which paths anchor | Ticket analysis **and** issue planner **and** a drift view |
| Added output detail | `anchor_ref`, anchor-distance `confidence`, `complexity_drivers[]` — **no** phase breakdown |
| Anchor matching | Claude picks from the injected list; **no** embedding/retrieval system |
| Authoring flow | Promote a past analysis (prefill from `ticket_analyses`) |
| No anchor fits | Fall back to today's bands, force low confidence, **say** no comparable was found |
| Driver taxonomy | **Fixed enum in code** (list still open, see below) |
| Day-one hours | **Hand-entered** — `ticket_actuals` prefill is a bonus for later |
| Anchor in Odoo HTML | **Internal only** — stripped from the customer-facing field |
| Slicing | **Three sequential plans** (drift view is plan 3, in scope) |

### Three findings that shaped this — verify before planning

1. **`ticket_actuals` is empty.** REVA's receiving endpoint shipped (migration
   040, `api/app/routes/v1/ticket_actuals.py`) but the Odoo-side sender did
   not — `docs/superpowers/specs/2026-07-15-ticket-actuals-sender-design.md` is
   still in the open-specs folder and ast-odoo went to reviewed-badge-timesheet
   instead. So promotion prefill has no data and the drift view renders
   "0 comparable" until that sender ships. Hence hand-entered hours.
2. **Anchors can leak across customers.** `reva/ticket_formatter.py` renders the
   analysis as HTML for an Odoo HTML field; "anchored on H-1234, 8 h" would put
   another customer's ticket number in front of this one. Hence internal-only.
3. **The 2026-07-11 wave never shipped.** All 7 plans in
   `docs/superpowers/plans/` are genuinely open — zero code hits for
   `get_repo_precision_stats`, `commit_suggestions`, `issue_plans`,
   `describe_command`, `fix_command`. This work touches `reva/types.py`
   (`StoryEstimate`) but adds **no** `RepoConfig` key — only a global kill
   switch — so it does not collide with their four keys. Plans must say so.

### Proposed breakdown (agreed shape, base commit `86abddd`) — superseded

Kept for history; the work actually shipped as a single 12-task plan
(`docs/superpowers/plans/archive/2026-08-04-golden-estimates.md`), not this
three-plan split. Plan 1 below (DB table, API, TUI CRUD) did not ship — see
the storage reversal above. Plan 3 (the calibration view) is the still-open
Plan B, tracked at `docs/superpowers/specs/2026-07-15-estimate-drift-stats-design.md`.

- **Plan 1 — golden set + authoring.** Migration 045 + ORM model + writers
  (create/list/edit/retire; candidates query joining `ticket_analyses` ⟕
  `ticket_actuals`); `/api/v1/golden-estimates` + `/candidates` (master gate);
  TUI "Golden" tab with promote/edit/retire + demo fixtures. No model calls —
  shippable alone.
- **Plan 2 — anchored estimates.** `StoryEstimate` gains `anchor_ref` /
  `anchor_confidence` / `complexity_drivers[]` + the fixed enum; anchor-block
  builder in shared `reva/` (load active, cap, **nonce-fence**, ops events) with
  `REVA_GOLDEN_ESTIMATES` + `REVA_GOLDEN_ESTIMATE_LIMIT` (default 30); both
  prompts rewritten onto one shared calibration source; formatter strips the
  anchor from the Odoo HTML; `contracts/` regen + ast-odoo re-sync.
- **Plan 3 — calibration view.** Implements the existing
  `2026-07-15-estimate-drift-stats-design.md` plus by-anchor and by-driver
  breakdowns; archive that spec on completion.

### Acceptance criteria (from the agreed ticket)

1. Operator can list analysed tickets not yet anchors and promote one, scope and
   hours prefilled where available.
2. An anchor cannot be saved without actual hours.
3. Anchors can be edited and retired; retired stops influencing estimates but
   stays visible in the calibration view.
4. The whole set is manageable from the TUI — no DB access needed.
5. With ≥1 active anchor, every story and issue estimate names its anchor and
   that anchor's hours.
6. Confidence reflects anchor distance, not model self-assessment.
7. Drivers come from the fixed enum; values outside it are rejected.
8. Both estimating paths share one anchor set and one calibration source.
9. With no usable anchor, estimates still come out — today's bands, low
   confidence, explicit "no comparable found".
10. The feature can be switched off entirely, restoring today's behaviour byte
    for byte.
11. Every degradation (anchors unavailable, set capped) logs **and** records an
    ops event — visible in `GET /api/v1/ops-events` and the TUI Failures tab.
12. Calibration view shows estimated vs. booked hours, by anchor and by driver.
13. Nothing customer-facing carries the anchor reference.

**Out of scope:** REVA editing its own bands or opening PRs against the prompts;
automatic promotion without operator review; anchors for work REVA never
analysed; phase-level hour splits; changes to the Odoo estimate mirror contract
(`contracts/inbound/update-issue-estimate.schema.json`) or the board schema.

### Owed before Plan 2 can be written — resolved

The driver enum shipped exactly as proposed below. Anchors are shared across
all Odoo instances (one file, no scoping). The "how many anchors before
switching on" question turned out moot: the feature is self-gating — a file
with an empty `anchors:` list renders bands-only, today's behaviour — so it
ships on with zero anchors and stays inert until the operator writes one.

- **The complexity-driver enum list.** Frozen in code, drift buckets are built
  on it, so a later change costs a migration. Proposal awaiting Joseph's edit:
  `data_migration`, `cross_module_workflow`, `new_model`, `report_layout`,
  `external_integration`, `access_rights`, `wizard_ui`, `computed_logic`,
  `scheduled_job`, `view_tweak`.
- **Open question:** are anchors shared across all Odoo instances or scoped per
  instance? Assumed **shared** (calibration reflects our dev speed, not a
  customer's) — this sets the table's unique constraint.
- **Open question:** how many anchors before switching on? Suggested ≥ 8
  covering all four bands.

---

## Addendum 2026-07-26 — support answers + shared code grounding

Spec/plan: `docs/superpowers/specs/archive/2026-07-25-support-answers-design.md`
and `docs/superpowers/plans/archive/2026-07-25-support-answers.md`.

Shipped: `POST /api/v1/support-request` (+ turn GET/requeue, thread drill-down),
personas (migration 043, master-key API, per-field resolution), support threads
and turns (migration 044), `SupportAnswerer` + `prompts/support_answer.md`, the
`reva-support-answer` and `reva-ticket-analysis` skills, and **planner-gated
code grounding shared by the support and ticket paths** (`needs_repo_code` on
`TicketKnowledge`; brakes `REVA_TICKET_CODE_GROUNDING` + per-repo
`code_grounding`). Workers 2 → 6. TUI Support + Personas tabs.

Three pre-existing bugs fixed on the way, found by an adversarial cross-check of
the plan against the code:
- ticket-analysis enqueued at 300s, which would SIGKILL an escalated CLI run
  mid-payment and re-pay on every retry — now derived from `REVIEW_JOB_TIMEOUT`,
  with `_STALE_PENDING` derived from that.
- requeue dropped `github_url`, silently downgrading grounding.
- the main ticket-analysis call never reached `claude_spend`, so the global cap
  could not see the ticket path's priciest leg. `SupportTurn` also joined
  `sum_instance_cost_since`.

**Owed:**
- **Staging validation** — nothing here has run against a live Claude CLI or a
  real Odoo. Highest-value first run: one support request that escalates
  (check `grounding_level: code`, the repo lock, and that no internal note text
  appears in the draft), and one ticket analysis that escalates (check no code
  identifiers leak into `summary`/`missing_info`).
- **Watch the planner gate.** If `needs_repo_code` fires indiscriminately,
  average ticket cost rises ~5-10x. Log the split per path on day one and pull
  a kill switch rather than weakening the shared planner prompt.
- **DB connections at 6 workers** — worst case 6 x 15 = 90 against
  `max_connections=100`, before api and scheduler. A watch item, reasoning in
  `docker-compose.prod.yml`.
- **ast-odoo**: contracts synced to `reva_contracts/` and the pin bumped to
  `db254db9…` on **main** (uncommitted). Its addon tests need an Odoo runtime
  and were not run here.
- **Odoo side not built**: the `reva_support_*` fields, the Ask REVA buttons,
  and the sender for `/api/v1/support-request` are all ast-odoo work.

---


**Updated:** 2026-06-14. Resume point.
**Replaces** the old slice-by-slice handoff (that described the original
Messages-API design and is now history in git).

---

## Addendum 2026-07-15 — ticket-actuals endpoint + audit odoo-flag fix

Two changes (spec: `docs/superpowers/specs/archive/2026-07-15-ticket-actuals-design.md`):

- **`POST /api/v1/ticket-actuals`** — Odoo pushes per-ticket timesheet totals
  when a ticket is marked done (actuals half of the estimate-calibration loop
  C1). Migration 040, `TicketActual` model, latest-wins upsert, journey event
  `actuals_reported`. Contracts regenerated (`8fc96171…`) and synced to
  ast-odoo + pin bumped (uncommitted there, rides the connector's next wave).
  All suites green incl. the real-Postgres tier.
- **Audits now honor `.claude-review.yml`'s `odoo` flag** (CORR-4 follow-up):
  loader extracted to `worker/repo_config.py`, read at the default branch.

**Owed:** ast-odoo sender on ticket-done — designed, see
`docs/superpowers/specs/2026-07-15-ticket-actuals-sender-design.md`; C1
second half (estimate drift stats, display-only locked) — designed, see
`docs/superpowers/specs/2026-07-15-estimate-drift-stats-design.md`; owed ops
items packaged in `docs/ops-debt-runbook-2026-07.md` (manual execution).

---

## Addendum 2026-07-14 — ticket analysis grounded in customer repo docs

Implemented per `docs/superpowers/specs/archive/2026-07-14-ticket-repo-docs-grounding-design.md`
(plan: `docs/superpowers/plans/archive/2026-07-14-ticket-repo-docs-grounding.md`; how it
works: `docs/technical.md` → "Ticket knowledge grounding"). Migration 039,
`reva/repo_docs.py`, `ticket_knowledge` restructure (one planner call feeds the
core block AND a new repo-docs block), new *Existing Customizations* output
section (prompt v2.10), `repo_docs_sections_used` surfaced through API + TUI.
All suites green incl. the real-Postgres integration tier.

**Owed:**
- **Staging validation** (model behavior): one real ticket against a repo with
  addon READMEs — check the new HTML section, `repo docs:N` in the TUI, and the
  second-run `fresh` fast path.
- ~~**Possible follow-up:** `core_knowledge.search_docs` still has the
  pre-existing AND behavior.~~ **Resolved 2026-07-25:** `search_docs` now
  matches `search_repo_docs` (OR-of-terms, one `plainto_tsquery` per term,
  `ts_rank` over the OR'd query; SQLite fallback OR'd too). A single
  `plainto_tsquery` ANDed all terms and near-never matched a realistic
  many-term planner query, which silently emptied the *Standard Odoo Coverage*
  block on most tickets. Covered by `test_search_docs_matches_any_term`
  (unit) + `test_core_docs_fts_or_of_terms_realistic_planner_query` /
  `test_core_docs_fts_scopes_to_version` (real-Postgres tier).

---

## 🚀 RESUME HERE — Tiers 0–2 done, Tier 3 started; **next: Tier 3 step B**

**⏭️ TOMORROW: implement Tier 3 feature B — per-repo learned memory.** Everything it
needs is already in place (statistic + TUI). Cold-start brief:
- **What:** read the per-(repo, category) dismiss data, build a short "what this team
  tends to reject" block, and inject it into the review prompt so REVA stops re-raising
  findings a team keeps dismissing. Down-weight, don't hard-suppress (that's what `/mute` is for).
- **Input (already shipped):** `GET /api/v1/metrics/learning` →
  `queries.metrics.learning_stats` → per (repo, category): `findings`, `dismissed`,
  `resolved_by_fix`, `still_open_at_merge` (90-day window). High `dismissed/findings` is the signal.
- **Where it plugs in:** the reviewer assembles `skill_params` in `worker/worker/reviewer.py`
  (see how `stated_intent` / `manifest_audit` are added); add a `learned_memory` param built from
  a new `RepoLookup` method, plus guidance in `prompts/review_guidance.md` (→ prompt **v1.8** — v1.7
  was already consumed by the Tier-2 hash re-baseline; bump the CHANGELOG so the drift guard doesn't
  alert). Nonce-fence nothing here — it's REVA's own data.
- **Plan/measure:** write the slice into `docs/tier3-plan.md` (feature B section), and watch the
  dismiss rate fall on the **TUI Feedback tab (`9`)** as it takes effect.

**All work is on `main`** (see `git log`); everything below is committed **and pushed**.

**Authoritative docs (read these first):**
- `FEATURE_ROADMAP.md` — the 6-tier roadmap (root).
- `docs/tier3-plan.md` — **Tier 3 plan + feature A (done) + the B brief**; implement B from here.
- `docs/tier2-staging-runbook.md` — what to do before live-testing Tier-2 features 4–9.
- `docs/tier0-plan.md`, `docs/tier1-plan.md`, `docs/tier2-plan.md`, `docs/tier2-detailed-plans.md` — per-tier plans + decisions.
- **`CLAUDE.md` changed this session** — new working rules: #1 *treat docs as possibly stale, verify
  against code*; #5 *keep the TUI in sync*; a definition-of-done (run every touched service's suite +
  ruff, `go test` for `tui/`); migration conventions. Read them before resuming.

**Shipped & committed (tests green: worker 615, api 154, scheduler 28; ruff clean; `tui` go test green):**
- **Tier 0 (done):** per-repo `block_on_severity` gating; deterministic Odoo severity calibration;
  `reva-risk-*` PR labels; trivial-diff short-circuit (+ a reorder false-skip fix).
- **Tier 1 (done):** prompt-version registry + drift guard; per-finding outcome ledger (migration
  `015`); feedback capture via `pull_request_review_thread`.
- **Tier 2 (9 of 9 done):** force-push/rebase delta-base guard; **delta-aware finding suppression**
  (the headline bug fix); test-coverage gate; **intent-grounded review** (4); **`__manifest__.py`
  validator** (5, `reva/odoo_manifest.py`); **second-pass self-critique** (6, default-off behind
  `REVA_VERIFY_HIGH_COST`); **migration-safety skill** (7, `reva-migration-review`); **XML/QWeb skill**
  (8, `.xml` un-stripped + `reva-xml-review`); **security-model consistency** (9, cross-check + floor).
  Routing is centralized in `reviewer._select_skill` (migration > delta > xml-only > diff/full).
- **Ops:** multi-worker (RQ) confirmed distributing jobs across replicas. `review_runs.worker_id` is
  now populated (RQ `hostname.pid`, else container hostname) for per-replica attribution — it was
  blank before. Two known "P0" bugs turned out already fixed (see the roadmap exclusions).

**Tier 3 (self-improvement) — STARTED** (`docs/tier3-plan.md`):
- **Feature A done:** `/dismiss`, `/mute <category>`, `/unmute` inline-comment commands —
  structured, zero-Claude-cost replies that short-circuit the paid reply. `/dismiss` writes a
  negative `review_feedback` row; `/mute` writes the new `muted_categories` table (migration `016`)
  and `reviewer._drop_muted_findings` suppresses those categories before posting.
- **Learning statistic + TUI shipped (the input/measurement for B):**
  `GET /api/v1/metrics/learning` (per repo×category dismiss/fix counts) + `GET /api/v1/metrics/mutes`,
  rendered in the new **TUI Feedback tab (`9`)**.
- **Next: feature B — per-repo learned memory** (see the cold-start brief at the top).

Still **run the live-CLI staging gate** for the Tier-2 review features (below) before trusting
their output in prod.

**Owed staging validation (live-CLI gate, A1/A2 pattern) for Tier 2 features 4–9** — unit suites prove
plumbing/routing only; review *quality* must be validated on a real Odoo repo. **Step-by-step setup +
per-feature pass/fail scenarios: [`docs/tier2-staging-runbook.md`](docs/tier2-staging-runbook.md).**
Summary of what to watch:
- (4) intent-grounding false-positive rate; (5) the LLM `depends`/version judgement + how the model
  merges the deterministic `manifest_audit` param; (6) the self-critique **false-drop rate** (enable
  `REVA_VERIFY_HIGH_COST` on staging first); (7) migration checks (destructive DDL, idempotency, JSONB);
  (8) xpath/`inherit_id` resolution (false positives on valid xpath); (9) detection quality — esp. **not**
  false-flagging the `_inherit`-extension pattern (the most common Odoo change).

**Prompt versioning:** Tier-2 prompt/skill edits landed under CHANGELOG version **v1.6**, then a
Tier-2 hash re-baseline bumped the set to **v1.7** (commit 55c049b). The next prompt change
(feature B's `review_guidance.md` block) starts **v1.8** — add a CHANGELOG heading and update the
`test_get_version` assertion, or the Tier-1 drift guard alerts on boot.

**Operator actions owed (not code):**
- **Enable the `Pull request review thread` webhook event** on the GitHub App — until then Tier-1
  feedback capture is dormant (no permission change needed; uses `Pull requests: Read`).
- The features 1–2 bug fix is partly prompt-quality (`reva-delta-review.md` guidance) — **validate on a
  real re-review PR on staging** before fully trusting it (the A1/A2/CodeGraph live-gate pattern).

**Local venvs:** `api/.venv` and `scheduler/.venv` were created this session; if missing, recreate per
"Running tests". `ruff` was pip-installed into `worker/.venv` for local linting.

---

## ⚠️ Read first — resuming on another workstation

The 2026-06-14 work above is on `main`. On a new workstation: `git clone`/`git pull`, then recreate
the per-service venvs (see "Running tests"). The pre-2026-06-14 baseline below was in sync at `499da2e`.

---

## Where things stand

**Phase 1 — reliability/security hardening (R1–R16): DONE & committed** (`a3343cf`),
tests-first. Highlights: RQ/CLI timeout alignment, git-op timeouts, stale-`running`
reaper, idempotent GitHub posting, webhook processed-flag idempotency, poller
`FOR UPDATE SKIP LOCKED`, advisory-locked budget, fail-closed API auth, CI
lint/type/coverage, backup script, prod-compose healthchecks/limits, `/health`
checks Redis, SSRF allowlist, pagination caps, rate limiter, owner/name validation.
Full detail: **`docs/production-readiness-plan.md`** (every item marked ✅).

**Phase 2 — gap-analysis follow-ups:** see **`docs/production-readiness-plan-phase2.md`**
(per-item Why/How/status). State:

| Item | Status |
|---|---|
| **A1** drop `--dangerously-skip-permissions`, output-in-clone | ✅ done, live-CLI verified (`a2e9030`) |
| **A3** ground-check findings vs the clone | ✅ done (`0ee3588`) |
| **A4** audit-log admin actions (`admin_audit` table) | ✅ done (`7b51e78`) |
| **A2** worker egress allowlist (proxy sidecar) | ✅ code done & tested; **infra (`egress-proxy/` + `docker-compose.egress.yml`) needs STAGING validation** (`43097b6`) — see `docs/egress-lockdown.md` |
| **C1–C3** free $0 security scanning (Dependabot/gitleaks/Semgrep) | ✅ done (`5ae6a09`); advisory until first run reviewed — see `docs/security-scanning-setup.md` |
| **E** CodeGraph engine layer | ✅ **spiked + implemented behind flag** (`REVA_CODEGRAPH_ENABLED`, default off); spec updated with live results. **Next = staging validation on a real PR** (below) |
| **D1–D2** integration/e2e (testcontainers) | 📋 not started — closes the Postgres-only coverage gap (SKIP-LOCKED/advisory-lock untested on SQLite) |
| **B2/B3** OTel tracing, Prometheus/Grafana · **D3/D4** · **F** data governance | 📋 later |
| **B1** error tracking (GlitchTip/Sentry) | ⏸️ backlog (parked by decision) |
| **E1/E2** human repo-overview, feedback/eval capture | ⏸️ out of scope / deferred |

Test counts (at the time of Phase 2): worker 267 · api 75 · scheduler 16.

---

## Recent work (2026-06-03 → 06-05) — on `main`, deployed locally, NOT yet in prod

Test counts now: **worker 339 · api 98 · scheduler 27**, ruff clean, **CI green**.

- **Repo audits — completed.** Findings are persisted (`audit_findings` table) and **major/critical are opened as GitHub issues** (`[REVA audit] …`, auto-created `reva-audit` label, hidden-marker dedup). Read via `GET /api/v1/audit-findings` + the new **TUI Audits tab (`8`)**; trigger from the **Repos tab (`a`)** or the API. Audits run on the **deep model**. Requires GitHub App **Issues: Read & write**.
- **Comment auto-resolution — fixed.** Backfill used `/pulls/{pr}/reviews/{id}/comments` (returns `line:null`), so `github_comment_id` was never stored and delta re-reviews resolved nothing (Aurium #60). Now uses the PR-level `/pulls/{pr}/comments` endpoint filtered by `pull_request_review_id`. Added `finding_comment_ids_*` / `delta_resolution_*` logs.
- **Models env-configurable.** Single source `reva/config.py`: `REVA_DEFAULT_MODEL` (`claude-sonnet-5`, changed from 4-6 in commit 590fb28), `REVA_DEEP_MODEL` (**`claude-opus-4-8`**, bumped from 4-7). Wired through both compose files. Audits + `/deep-review` use the deep model.
- **CodeGraph enabled** on the live worker (`REVA_CODEGRAPH_ENABLED=true`) + a positive `codegraph_index_ready` log. **Still owed (HANDOFF's standing CodeGraph gate): confirm the model actually calls `mcp__codegraph__*` on a real full/deep PR.**
- **nginx → Cloudflare tunnel.** Plain HTTP on `127.0.0.1:8080`; TLS at the Cloudflare edge; real client IP via `CF-Connecting-IP`. Dropped certbot / Let's Encrypt / `:443` / `setup-letsencrypt.sh`. Added a branded cloud **404**. Prod compose + `docs/setup-production.md` rewritten for the tunnel.

**👉 What's next — see [`docs/next-steps-plan.md`](docs/next-steps-plan.md)** (per-item P0/P1/P2 plans + a pre-deploy live-test checklist). Highest value: **deploy to the server and run the live-test checklist** — the only way to prove the tunnel, CodeGraph-in-use, the resolution cycle, and audits end-to-end. (The two **P0** bugs once listed here — comment commands on unknown PRs, and the deep→diff downgrade CORR-7 — are now both fixed; see the resume section above.)

---

## CodeGraph (Phase-2 E) — spiked + implemented (2026-06-01)

**Spike done** in the worker container. Results (full detail in the spec's "Spike
results" section): pinned **0.9.8** (npm `@colbymchenry/codegraph`); on a real
Odoo repo (OCA/server-tools, 464 Py files) indexing took **4s**; a steered review
was **~26% cheaper / ~72% fewer tool calls** than grep-only. Corrected the spec's
wrong guesses — it's `codegraph init`/`sync` + `codegraph serve --mcp` (not
`codegraph mcp`). **Key finding: steering is mandatory** — without a prompt note
the model ignores the tools and greps.

**Implemented (behind `REVA_CODEGRAPH_ENABLED`, default off):** `_codegraph_prepare`
+ gating in `reva/claude_code_runner.py` (repo-aware skills only: `reva-full-review`,
`reva-repo-audit`; diff/delta excluded; fall back on any index/setup failure);
settings (`worker/worker/settings.py`); Dockerfile pin; steering notes in the two
repo-aware skills; `.env.example`. Unit tests cover the gating matrix + fallback.

**👉 Next = staging validation on a real PR:** set `REVA_CODEGRAPH_ENABLED=true`
on a staging worker, run a `full`/`deep` review on one repo, confirm the model
calls `mcp__codegraph__*` and the review still completes; then enable for `deep`
first, then `full`/`audit`. Keep `diff` off CodeGraph. (Same live-CLI gate as A1/A2.)
If you also run reviews under the A2 egress lock, the codegraph MCP server is a
local stdio subprocess (no egress) — nothing to allowlist.

---

## Review scope & triggers (2026-06-02)

Shipped this session (all on `main`):

- **`/review-all`** comment command → `diff-all` mode: a diff-depth review (Sonnet)
  over **all** changed files, not just `custom_addons/`. Reuses the command →
  `review_mode` channel; reviewer passes `include_prefixes=()` for that mode.
- **Default diff cap raised 1000 → 2500 lines** (`DEFAULT_MAX_DIFF_LINES`). Token
  guard (60k) and per-repo `.claude-review.yml max_diff_lines` override unchanged.
- **Test-dir exclusion: tried then reverted** (`323b716` → `a21adc9`). Decision:
  **keep test files in all modes.** Don't re-add a global `tests/` skip; if a repo
  wants it, use `skip_paths: ["*/tests/*"]` in its `.claude-review.yml`.

**✅ Resolved — comment commands on unknown PRs.** On a DB miss `_handle_issue_comment`
now fetches the PR from the GitHub API and upserts it (`_fetch_and_upsert_pr`,
`webhooks.py`), so `/review` / `/review-all` work on any open PR without a reopen/push
(fixed in `004cd5c`, tested). The CORR-7 `deep→diff` downgrade is likewise fixed
(`_MODE_PRECEDENCE` guard in `upsert_pending_review`, `11e601a`).

---

## Operator to-dos still owed (not code)

- **Enable Dependabot** alerts + security updates: repo → Settings → Code security
  and analysis. (`docs/security-scanning-setup.md` §1.)
- **After the first CI run:** review gitleaks/Semgrep output, add allowlist entries
  for test placeholders, then delete the two `continue-on-error: true` lines in
  `.github/workflows/ci.yml` to make them blocking.
- **A2 egress overlay:** validate in staging per `docs/egress-lockdown.md` before
  using in prod (allowed host works / disallowed blocked / a real review still
  completes). If you enable the **ticket-analysis** tool, add the Odoo host to
  `egress-proxy/filter`.

---

## Decisions log (this session)

- **Topology:** single instance now, **scaling soon** → concurrency/idempotency treated as real.
- **R7 budget cap:** bounded overshoot via Postgres advisory lock (not a hard reservation).
- **A2 egress:** proxy sidecar (over fragile iptables CIDR rules — Anthropic IPs rotate).
- **E3 / CodeGraph:** **engine layer** (cheaper/repo-aware reviews via MCP), not a human dashboard; spike-first.
- **B1 error tracking:** backlog. **CD:** handled by odoo.sh. **Backups:** deferred till past testing.
- **Rejected:** committable suggested-fixes (juniors learn by doing), change "walkthrough" summaries.
- **Tooling:** keep it lean / few new services; self-host over SaaS; $0 on GitHub Team.
- **TLS/ingress (2026-06-05):** Cloudflare tunnel — TLS at the edge, nginx plain-HTTP on `127.0.0.1:8080`, no certs/certbot. Over the old Let's Encrypt+`:443` setup.
- **Deep model (2026-06-05):** `claude-opus-4-8` (from 4-7), env-overridable; **audits always use the deep model**.
- **Audits (2026-06-05):** **manual-only** (API/TUI trigger); periodic/cron audits deferred — deliberate, revisit if needed.

---

## Orientation (architecture & where things live)

Flow: GitHub webhook → `api` verifies + upserts `pending_reviews` (debounce) →
`scheduler` poller enqueues RQ → `worker` clones the repo & runs the **headless
`claude` CLI** (Sonnet/Opus) → posts Check Run + PR Review → Postgres for
analytics; Go/Bubble-Tea `tui` reads the internal `/api/v1`.

- **Authoritative docs:** root `README.md`, per-module `*/README.md`,
  `docs/superpowers/specs/`, and the two `docs/production-readiness-plan*.md`.
  (The legacy numbered `doc/00–13` tree has been removed.)
- **Shared lib:** `reva/` (types, clients, db, `claude_code_runner.py`, formatters).
- **Run tests** (per service, Python 3.14, each installs `reva` editable):
  ```bash
  cd worker && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest tests/   # 339
  cd ../api && … pytest tests/        # 98
  cd ../scheduler && … pytest tests/  # 27
  ```
  Lint: `ruff check reva worker/worker api/app scheduler/scheduler`. Go TUI: `cd tui && go test ./...`.
- **Live-CLI validation pattern:** A1/A2 were verified by running the real `claude`
  CLI in the worker container (the sandbox can't). CodeGraph's spike is the same idea.
