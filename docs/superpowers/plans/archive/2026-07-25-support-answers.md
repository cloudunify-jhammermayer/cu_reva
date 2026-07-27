# Support Answers + Shared Code-Grounding Escalation — Implementation Plan

Spec: `docs/superpowers/specs/2026-07-25-support-answers-design.md` (read it
first — every decision below is justified there, not here).

**Scope note:** the planner-gated CLI escalation (Task 5) is deliberately
**shared** with ticket analysis (Task 10), not support-only. Both are
Odoo-facing analysis paths whose *output* is business-level but whose
*evidence* often isn't; two divergent grounding strategies would be two things
to tune and two ways to be wrong.

**Prerequisites already shipped.** (1) 2026-07-25 —
`core_knowledge.search_docs` now uses OR-of-terms, matching
`search_repo_docs`; without it the `/core` block was empty on most realistic
planner queries. (2) 2026-07-26, **Phase 0** — three pre-existing gaps that the
CLI escalation would have walked into, found by an adversarial cross-check of
this plan against the code:

- `api/app/routes/v1/ticket_analyses.py` now enqueues at `REVIEW_JOB_TIMEOUT`
  (was 300s — an escalated CLI run would be SIGKILLed mid-payment and `_RETRY`
  would re-pay twice more), and `_STALE_PENDING` is **derived** from it so a
  stale-requeue can't start a second paid run beside a live one.
- Requeue replays `github_url` (it was silently dropped, downgrading grounding
  and — once escalation lands — making a requeued analysis unable to escalate)
  and 409s on retention-purged rows instead of re-analysing the sentinel.
- `record_ticket_analysis_completed` records the main call in `claude_spend`.
  The global rolling cap reads **only** that ledger, so the ticket path's
  priciest leg was invisible to it; only the planner leg was recorded.

**Phase 2 landed 2026-07-26** — Tasks 5–10: the `needs_repo_code` planner
gate (plumbed through `TicketKnowledge`, and representable even when the
docs aren't worth searching), `SupportAnswerer` + `support_answer.md`, the
`reva-support-answer` and `reva-ticket-analysis` skills, the HTML formatter,
`support_runner` + RQ shim + context wiring, and the same escalation on the
ticket path behind the `REVA_TICKET_CODE_GROUNDING` global switch and the
per-repo `code_grounding` flag. `answer_html` was renamed to `answer` on the
model (the DB column keeps its name — it stores rendered HTML), and
`_NO_REVIEW_GUIDANCE_SKILLS` stops findings governance being prepended to
skills that emit no findings.

**Phase 1 landed 2026-07-26** — Tasks 1–4: migrations 043 (`personas`) and 044
(`support_threads`/`support_turns`) with their ORM models and writers,
`reva/persona.py` (per-field resolution + deterministic prompt block), and the
support types + `submit_support_answer` tool schema with the `cannot_answer`
model validator. `SupportTurn` was added to `sum_instance_cost_since` at the
same time, closing the per-instance half of the budget gap. Real-DDL coverage
for both migrations is in the Postgres tier.

Still open from the cross-check, folded into the tasks below: `SupportTurn`
must join `sum_instance_cost_since` (Task 6/12), `needs_repo_code` needs real
plumbing through `TicketKnowledge` rather than a returned dict key (Task 5),
the support dedup key must include `field_name` (Task 6), and Task 13 ships
with a kill switch. Note `repo_lock` is an flock on a **shared volume** —
correct across containers on one host only; state that as a deployment
invariant when scaling to 6 workers.

Every task follows the repo's TDD loop: write the failing test, run it, watch
it fail for the right reason, implement, run it green. Tasks 1–10 are worker/
shared; 11–13 are API/TUI; 14–15 are cleanup. Tasks 1–4 are independent and can
land in any order; 5–10 depend on them. Task 10 (ticket analysis) reuses the
branch pattern Task 9 establishes, so land 9 first.

**Definition of done for the whole slice:** `make test` green (worker, api,
scheduler — this touches shared `reva/`), `make test-integration` green,
`ruff check reva worker/worker api/app scheduler/scheduler` clean,
`cd tui && go build ./... && go vet ./... && go test ./...` green, and
`contracts/` regenerated + synced to ast-odoo with the pin bumped.

---

### Task 1: Migration 043 + `Persona` model + writers

Files: `db/migrations/043_personas.sql`, `reva/db/models.py`,
`reva/db/writers.py`, `worker/tests/test_db.py`.

- [ ] **Step 1: Write the migration.** Idempotent (`CREATE TABLE IF NOT
  EXISTS`), `id BIGSERIAL PRIMARY KEY` (match the existing files, not
  `GENERATED … IDENTITY`). Columns: `scope TEXT NOT NULL` (`'default'|'repo'`),
  `repo_full_name TEXT` (NULL for default), `language TEXT`, `formality TEXT`,
  `technical_depth TEXT`, `length TEXT`, `salutation TEXT`, `sign_off TEXT`,
  `style_notes TEXT`, `content_policy TEXT`, `active BOOLEAN NOT NULL DEFAULT
  true`, `created_at`/`updated_at TIMESTAMPTZ`. Partial unique index on
  `repo_full_name WHERE scope = 'repo'`, and a second partial unique index
  `WHERE scope = 'default'` so there is at most one default row.
- [ ] **Step 2: Add the ORM model** `Persona` in `reva/db/models.py` mirroring
  the migration exactly (tests build from the models, so a drifted model means
  the tests pass against a schema production doesn't have).
- [ ] **Step 3: Failing writer tests** in `test_db.py`: `upsert_persona`
  round-trips; `get_persona(repo_full_name)` returns the repo row;
  `get_persona(None)` returns the default row; the repo partial unique index
  rejects a second row for the same repo; `list_personas` returns default first.
- [ ] **Step 4: Run → FAIL. Implement the writers. Run → PASS.**
- [ ] **Step 5: Postgres tier** — add the migration-DDL assertion to
  `worker/tests/test_pg_integration.py` (mirror
  `test_migration_039_creates_repo_docs_tables_and_column`), and add `personas`
  to the `pg_db` fixture's `TRUNCATE` list.

### Task 2: Persona resolution

Files: `reva/persona.py` (new), `worker/tests/test_persona.py` (new).

Per-field resolution: `default` row < repo row (NULL fields inherit) < Odoo
`persona_context` (additive free text, never overrides the knobs).

- [ ] **Step 1: Failing tests.** Repo row with NULL `formality` inherits the
  default's; repo row with a set value wins; no repo row at all → pure default;
  no default row either → hardcoded fallback (formal, `language: "auto"`,
  standard length) rather than an empty prompt block; `persona_context`
  appends and does **not** clear `content_policy`; the rendered block is
  deterministic (same input → byte-identical output, so prompt caching works).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `resolve_persona(db, repo_full_name,
  persona_context) -> ResolvedPersona` plus `render_persona_block()`. The
  resolved object is persisted verbatim as `support_threads.persona_snapshot`
  so a thread's tone is auditable after someone edits the persona.
- [ ] **Step 4: Run → PASS.**

### Task 3: Migration 044 + `SupportThread` / `SupportTurn` + writers

Files: `db/migrations/044_support_threads.sql`, `reva/db/models.py`,
`reva/db/writers.py`, `worker/tests/test_db.py`.

- [ ] **Step 1: Migration.** `support_threads` — `odoo_instance_id BIGINT
  REFERENCES odoo_instances(id)`, `ticket_id INT NOT NULL`, `model_name TEXT
  NOT NULL`, `github_url TEXT`, `persona_snapshot JSONB`, `status TEXT`,
  timestamps; UNIQUE (`odoo_instance_id`, `ticket_id`, `model_name`).
  `support_turns` — `thread_id BIGINT REFERENCES support_threads(id)`, `seq
  INT`, `question TEXT`, `answer_html TEXT`, `result_structured JSONB`,
  `request_kind TEXT`, `answer_status TEXT`, `grounding_level TEXT`
  (`docs|code|none`), the four token columns + `estimated_cost_usd
  NUMERIC(12,6)`, `job_id TEXT`, `status TEXT`, `error_message TEXT`,
  `callback_sent_at`, `callback_error`, `created_at`, `completed_at`. UNIQUE
  (`thread_id`, `seq`); partial unique index on one pending turn per thread
  (mirrors `idx_ticket_analyses_pending` — it's what makes the submit dedup
  race-safe).
- [ ] **Step 2: ORM models.** Same drift warning as Task 1.
- [ ] **Step 3: Failing writer tests.** `get_or_create_support_thread` is
  idempotent on (instance, ticket, model) and assigns `seq` monotonically;
  `record_support_turn_created` → pending; completed/failed transitions;
  `get_pending_support_turn` backs the dedup; `reset_support_turn` for requeue;
  `prior_turns(thread_id, limit)` returns oldest-first for prompt replay.
- [ ] **Step 4: Run → FAIL. Implement. Run → PASS.**
- [ ] **Step 5:** DDL assertion in the Postgres tier + both tables into the
  `pg_db` `TRUNCATE` list.

### Task 4: Types + output tool schema

Files: `reva/types.py`, `reva/support_tool.py` (new),
`worker/tests/test_support_tool.py` (new).

- [ ] **Step 1: Add the models** to `reva/types.py`, beside the ticket types:
  `ChatterEntry` (`id`, `posted_at`, `author`, `author_kind`, `visibility`,
  `body`), `SupportJobParams` (`turn_id`, `thread_id`, `odoo_instance_id`,
  `ticket_id`, `model_name`, `field_name`, `subject`, `question`, `github_url`,
  `persona_context`, `chatter: list[ChatterEntry]`, `attachment`), and
  `SupportAnswerResult` matching the spec's output schema —
  `request_kind`, `answer_status`, `answer`, `cannot_answer_reason`,
  `open_questions`, `sources`, `handoff`, `language`, `confidence`.
- [ ] **Step 2: Model validator** — `cannot_answer_reason` is required when
  `answer_status == "cannot_answer"`, and `answer` must be empty in that
  case. This is the schema-level half of the "no caveated draft" decision;
  don't rely on the prompt alone.
- [ ] **Step 3: Failing tests** for the validator both ways, plus
  `build_support_tool_schema()` producing `additionalProperties: False` +
  `strict: True` (mirror `reva/ticket_tool.py`).
- [ ] **Step 4: Run → FAIL. Implement. Run → PASS.**

### Task 5: Planner extension — `needs_repo_code`

Files: `prompts/core_query_planner.md`, `reva/ticket_knowledge.py`,
`worker/tests/test_ticket_knowledge.py`.

The planner call already exists and already gates retrieval; this adds one
boolean to its tool schema so a runner can choose docs vs CLI. **Shared by both
the support path (Task 9) and ticket analysis (Task 10)** — the gate is written
once, in the planner both already call.

- [ ] **Step 1: Failing tests.** `plan_core_queries` returns
  `needs_repo_code=True` when the tool says so; defaults to `False` when the
  key is absent (older prompt / partial output — never escalate by accident);
  a planner failure still degrades to `(None, cost, error)` exactly as today.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** Add `needs_repo_code` to `PLANNER_TOOL`'s
  `input_schema` + `required`, thread it through the returned plan dict, and
  add the guidance to `prompts/core_query_planner.md`: set it when answering
  needs the project's *code or configuration*, not when the official docs or
  the repo's own markdown would do.
- [ ] **Step 4: Run → PASS**, plus the existing ticket-analysis tests. The
  planner is shared, so its return shape changes for both callers — but
  ticket-analysis *behavior* must not change until Task 10 wires the branch
  (the new key is read by nobody yet).
- [ ] **Step 5: Prompt versioning** — bump `prompts/CHANGELOG.md` so the drift
  guard doesn't alert.

### Task 6: `SupportAnswerer` (Messages API path) + prompt

Files: `reva/support_answerer.py` (new), `prompts/support_answer.md` (new),
`worker/tests/test_support_answerer.py` (new).

- [ ] **Step 1: Failing tests** (httpx MockTransport, mirroring
  `test_ticket_analyzer.py`): happy path returns a validated
  `SupportAnswerResult`; the question, every chatter entry, and any attachment
  are nonce-wrapped and labelled untrusted; **internal-visibility entries land
  in a separate fenced block carrying the never-quote instruction** and public
  ones do not; prior turns replay oldest-first; the persona block is a
  `cache_control` system block and the volatile content sits after it; no tool
  call → `PermanentError`; `stop_reason == "max_tokens"` →
  `MalformedModelOutput` (checked before the None-input case, same ordering
  bug the ticket analyzer guards against); `max_tokens=16384`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the answerer and the prompt. The prompt states:
  REVA's identity, the answer is a **draft for a consultant** (not sent to the
  customer as-is), the same-language rule, the persona/content-policy
  contract, the internal-notes never-quote rule, the source-citation
  requirement on answered drafts, and the `cannot_answer` contract — say what's
  missing, do not produce caveated prose.
- [ ] **Step 4: Run → PASS.** Register the prompt in `prompts/CHANGELOG.md`.

### Task 7: `reva-support-answer` skill (CLI path)

Files: `prompts/skills/reva-support-answer.md` (new),
`reva/claude_code_runner.py` (CodeGraph allowlist), `worker/tests/`.

- [ ] **Step 1:** Write the skill. Same output contract as Task 6 (it writes
  the `SupportAnswerResult` JSON to the temp output path), plus explicit
  exploration guidance: follow the chain from symptom → field → computation →
  custom-addon override → view, and prefer a justified "I could not find it,
  and here is where it would live" over a guess. Task parameters carry the
  persona block, the fenced chatter (public/internal split), prior turns, and
  the retrieved `/core` block.
- [ ] **Step 2:** Add the skill to the CodeGraph-enabled set — it is
  repo-aware, like full/deep/audit. Note this path does **not** go through
  `Reviewer._select_skill` (there is no diff to route on); the support runner
  names its skill directly.
- [ ] **Step 3: Failing test** that `ClaudeCodeRunner.review(skill=
  "reva-support-answer", …)` reads the skill file and nonce-fences every task
  parameter (mirror the existing runner tests with a fake CLI subprocess).
- [ ] **Step 4: Run → FAIL → implement → PASS.**

### Task 8: HTML formatter

Files: `reva/support_formatter.py` (new),
`worker/tests/test_support_formatter.py` (new).

- [ ] **Step 1: Failing tests.** `answered` renders answer + sources; `partial`
  renders the answer plus the open questions; `cannot_answer` renders the
  reason + open questions and **no** answer prose; internal-note content never
  appears in the output for any status; output passes `ensure_renderable`
  unchanged (well-formed); HTML in model output is escaped.
- [ ] **Step 2: Run → FAIL. Implement** (mirror `reva/ticket_formatter.py`'s
  badge/section style so the Odoo field looks consistent). **Run → PASS.**

### Task 9: Worker runner + tasks shim + context wiring

Files: `worker/worker/support_runner.py` (new),
`worker/worker/support_tasks.py` (new), `worker/worker/runner.py`,
`worker/tests/test_support_runner.py` (new).

Pipeline: reset Odoo status → instance budget check → resolve persona → plan →
branch (docs → `SupportAnswerer`; code → `repo_lock` + `ensure_repo` +
`ClaudeCodeRunner.review`) → format → persist → `write_field`.

- [ ] **Step 1: Failing tests** (SQLite + fakes, mirroring
  `test_ticket_runner.py`): docs path happy case; code path happy case
  (planner says `needs_repo_code`, `ensure_repo` + `review` called under the
  lock); **no App installation → falls back to docs path, marks grounding
  `docs`, records an ops event** (degradations must be visible); repo lock busy
  → `TransientError` (RQ retries, no partial write); over instance budget →
  failed turn + `PermanentError`, no Claude call; idempotent resume reuses the
  persisted HTML and goes straight to the callback without re-paying; callback
  failure records `callback_error` + an ops event and re-raises; `html_repaired`
  ops event when `ensure_renderable` fixes the render; thread `seq` increments
  across turns and prior turns are replayed.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the runner, the `terminal_on_permanent` shim in
  `support_tasks.py` (mirror `ticket_tasks.py`), and add
  `support_answerer: SupportAnswerer | None = None` to `WorkerContext` with a
  `None` default so existing fixtures stay valid; wire it in
  `build_worker_context`.
- [ ] **Step 4: Run → PASS**, plus the whole worker suite for regressions.

### Task 10: Ticket analysis — same planner-gated escalation

Files: `worker/worker/ticket_runner.py`, `prompts/skills/reva-ticket-analysis.md`
(new), `prompts/ticket_analysis.md`, `worker/tests/test_ticket_runner.py`.

Ticket analysis gets the identical branch, for the identical reason: the
*output* is business-level, but the *evidence* often isn't. **Existing
Customizations** is grounded only in addon READMEs today (frequently absent or
stale); **Standard Odoo Coverage** can be wrong when the customer has already
overridden the stock behavior; and **Missing Information** lists gaps that
aren't gaps once you can see the field already exists. One gate, one mental
model, one place to tune.

This is the first time the ticket path clones anything — today
`repo_core_version()` only reads `.claude-review.yml` over the GitHub API.

- [ ] **Step 1: Failing tests** (mirror Task 9's, in `test_ticket_runner.py`):
  planner says `needs_repo_code` → `ensure_repo` + `review` called under
  `repo_lock`, result still validates as `TicketAnalysisResult`; planner says
  no → today's Messages API path, byte-identical behavior; **`github_url` is
  None (project-less ticket) → never escalates**, docs-only, no lock taken; no
  App installation → docs-only + ops event; repo lock busy → `TransientError`;
  the existing idempotent-resume and malformed-output-retry paths still hold on
  both branches.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the branch in `run_ticket_analysis` and the
  `reva-ticket-analysis` skill. The skill writes the **same**
  `TicketAnalysisResult` JSON the Messages API path produces, so
  `format_ticket_html` and everything downstream is untouched.
- [ ] **Step 4: Guard the output prohibition — this is the real risk.** Having
  read the code, the model will want to cite it, but the analysis is for a
  product owner. Restate the no-code-level-detail rule in the skill (models,
  XML views, Python, field names are evidence, never output; addon names at the
  consultant level are fine, matching `prompts/ticket_analysis.md`), and add a
  test asserting no code identifiers leak into `summary` / `missing_info` on
  the CLI branch.
- [ ] **Step 5: Run → PASS**, plus the whole worker suite.
- [ ] **Step 6: Prompt versioning** — bump `prompts/ticket_analysis.md`'s
  version and `prompts/CHANGELOG.md`; add the skill to CLAUDE.md's skill list
  and CodeGraph-enabled set.

### Task 11: API — support-request endpoint + contract

Files: `api/app/schemas/support_requests.py` (new),
`api/app/routes/v1/support_requests.py` (new), `api/app/routes/v1/__init__.py`,
`api/app/queries/support_requests.py` (new), `reva/odoo_contracts.py`,
`contracts/`, `api/tests/test_v1_support_requests.py` (new).

- [ ] **Step 1: Failing tests.** The spec's example payload verbatim → 202 with
  `thread_id` + `turn_id` + job enqueued at the exact string path; a bad
  `github_url` → 422 and nothing enqueued (format-only, no reachability probe);
  an unsupported attachment extension → 422; `.md` accepted; dedup returns the
  same pending turn without a second enqueue; concurrent-POST race hits the
  partial unique index and returns the winner; GET is instance-scoped (another
  instance's thread 404s, not 403 — ids must not be probeable); requeue
  404/409/202; enqueue failure marks the turn failed and returns 503.
- [ ] **Step 2: Run → FAIL. Implement** (clone `ticket_analyses.py`'s
  structure: `_JOB_TIMEOUT`, `_RETRY`, `_FAILURE_TTL`, `_STALE_PENDING`).
  Mount the create route on the instance-key router and the read routes on the
  shared router.
- [ ] **Step 3: Run → PASS.**
- [ ] **Step 4: Contracts.** Add the `support-request` entry to `CONTRACTS` +
  `_inbound_models()`, run `python -m reva.odoo_contracts generate`, commit the
  regenerated `contracts/`, sync to ast-odoo and bump the pin in
  `cu_reva_connector/tests/test_contracts.py`.

### Task 12: Personas API (master key only)

Files: `api/app/schemas/personas.py`, `api/app/routes/v1/personas.py`,
`api/app/queries/personas.py`, `api/tests/test_v1_personas.py` (all new).

Two contracts this task must honor, both settled while building Phase 1:

- **`active=false` means "as if absent".** `resolve_persona` treats an inactive
  row as missing — an inactive repo row falls back to the default, an inactive
  default falls back to the hardcoded persona. So the API's deactivate action
  genuinely takes effect; do not add a second, different meaning here.
- **`language` is coupled across two models.** `SupportAnswerResult.language`
  is `Literal["de", "en"]` (strict tool schema, so the model cannot emit
  anything else), while `Persona.language` is free text accepting `"auto"`.
  Validate persona `language` against `{"auto", "de", "en"}` here — otherwise
  an operator can pin a third language, the prompt will honor it, and the
  model will be forced to mis-report the field. Widening to a third language
  means changing both.

- [ ] **Step 1: Failing tests.** List/create/update round-trip; **an Odoo
  instance key is rejected** (403/404) on every persona route — an instance
  must never be able to rewrite its own tone; creating a second `scope='repo'`
  row for the same repo → 409, not 500; unknown enum value for
  `formality`/`length`/`technical_depth` → 422; a `language` outside
  `{"auto","de","en"}` → 422; deactivating a persona is reflected by
  `resolve_persona` falling back.
- [ ] **Step 2: Run → FAIL. Implement** on the master-key router. **Run → PASS.**

### Task 13: TUI — Support tab + persona editing

Files: `tui/internal/api/{client,iface,mock,types}.go`,
`tui/internal/ui/support.go` (new), `tui/internal/ui/personas.go` (new),
`tui/internal/ui/app.go`, plus `_test.go` files.

- [ ] **Step 1:** Extend the API client with `SupportThreads`,
  `SupportThreadDetail`, `RequeueSupportTurn`, `Personas`, `CreatePersona`,
  `UpdatePersona` — reusing the existing `postJSON` helper that `AddRepo` and
  `CreateOdooInstance` already use.
- [ ] **Step 2:** New tab showing threads (repo, ticket, turns, last status,
  `request_kind`, grounding level) with drill-down into turns, plus a persona
  editor. Follow the existing tab/list/detail patterns; add the mock
  implementation so `--demo` still runs.
- [ ] **Step 3:** Table-driven tests mirroring `tickets_test.go`, plus an
  overflow test entry (`overflow_test.go` enumerates every view).
- [ ] **Step 4:** `go build ./... && go vet ./... && go test ./...` green.

### Task 14: `.md` attachments

Files: `reva/attachment_text.py`, `worker/tests/test_attachment_text.py`.

- [ ] **Step 1: Failing tests** — a `.md` attachment classifies and extracts as
  text; a non-UTF-8 `.md` raises the same `ValueError` as a bad `.txt`; the
  extension gate still rejects `.xlsx`/`.pptx`.
- [ ] **Step 2: Run → FAIL. Add `".md"` to `_ALLOWED_EXTENSIONS`** and route it
  through the existing UTF-8 text branch. **Run → PASS.**
- [ ] **Step 3:** Update the `Attachment` docstring in `reva/odoo_contracts.py`
  (it names the accepted set, and it's the generated contract's description) and
  regenerate contracts.

### Task 15: Docs

- [ ] **Correct the stale read-only TUI claims** — contradicted by
  `tui/internal/api/client.go`, which already ships `TriggerAudit`, `AddRepo`,
  the three `Requeue*` methods, `CreateOdooInstance`, `RotateOdooInstanceKey`,
  `SetOdooInstanceActive`, and `DeleteOdooInstance`. Three places:
  `CLAUDE.md:64` (working principle #5), `CLAUDE.md:106` (architecture
  pipeline), `tui/README.md:3`.
- [ ] **CLAUDE.md** — add `reva-support-answer` to the skills list and to the
  CodeGraph-enabled set; note that support-answer skill selection bypasses
  `Reviewer._select_skill`.
- [ ] **README.md** — a Support Answers section: what it does, the two
  grounding depths, the persona table, the Odoo fields, and the
  draft-not-send boundary stated explicitly.
- [ ] **`docs/technical.md`** — how persona resolution and the planner gate
  work, beside the existing "Ticket knowledge grounding" section.
- [ ] **HANDOFF.md** — resume point.
- [ ] **Move the spec + this plan into `archive/`** as part of the shipping
  change (the archive location is what marks work closed, not a `Status:` line).

---

## Risks to watch during implementation

- **Planner gate accuracy is the cost lever, and ticket analysis is where it
  bites.** Support answers are button-press-only and low volume; ticket
  analysis runs on effectively every ticket. If `needs_repo_code` fires
  indiscriminately, that's a locked CLI run per ticket against the shared
  per-instance daily budget. Log the split per path and check it on the first
  day of real traffic before tuning the prompt. If it needs a brake, prefer a
  per-repo or per-instance kill switch (mirroring the existing
  `.claude-review.yml` flags) over weakening the planner prompt.
- **Ticket analysis is the first clone on the ticket path.** Today it only
  reads `.claude-review.yml` over the API. Expect new repo-lock contention with
  PR reviews, and expect project-less tickets (`github_url` None — common) to
  never escalate, which is correct but means the benefit is uneven across
  customers.
- **Output prohibition under code grounding.** A model that has read the code
  will want to cite it, but the ticket analysis is for a product owner. Task 10
  Step 4 tests this explicitly; treat a regression there as blocking, not
  cosmetic.
- **Prompt caching only helps the docs path.** A long thread that escalates
  every turn re-pays context each time. If that shows up in the spend ledger,
  the fix is a sticky per-thread decision rather than per-turn — deliberately
  deferred until there's data.
- **Repo-lock contention** between support answers and PR reviews of the same
  repo is real but bounded by button-press-only triggering. Watch for
  `repo_lock … busy` TransientErrors in the ops events.
- **The internal-notes leak is the one that matters.** Task 6 and Task 8 both
  test it; keep both — the prompt rule and the formatter are independent
  failure points.
