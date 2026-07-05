# Odoo core knowledge + ops-event log — design

**Date:** 2026-07-05
**Component:** `cu_reva` — new core-knowledge layer (`reva/odoo_registry.py`, `reva/core_knowledge.py`, `scripts/core_sync.sh`), review-path wiring (`reva/claude_code_runner.py`, `worker/worker/reviewer.py`, prompts/skills), ticket-path wiring (`reva/ticket_analyzer.py`, `reva/types.py`, `prompts/ticket_analysis.md`), DB (registry tables + `ops_events` + `odoo_instances.odoo_version`), API (`/api/v1/ops-events`, instance PATCH), scheduler (retention), TUI (Failures tab second view, dashboard, tickets badge, Odoo tab), compose (read-only `/core` bind mount on the worker).
**Status:** Design approved (Q&A with Joseph, 2026-07-04/05), pending implementation plan.

## Problem

REVA reviews custom Odoo code and analyses Odoo tickets with **no knowledge of
Odoo core**. Consequences:

- PR reviews cannot flag code that **reimplements standard functionality**
  (custom approval flows where stock settings suffice, hand-rolled computes
  duplicating core mixins). `prompts/odoo19.md` has a handful of Info-level
  "prefer the stock feature" rules, but nothing systematic and no core source
  to check against.
- Ticket analyses cannot answer the consultant's first question — **"can this
  be configured instead of built?"** The current prompt actively forbids
  technical content (`prompts/ticket_analysis.md:7,55`) and the required
  `odoo_notes` schema field has no prompt guidance at all.
- The official Odoo documentation is not consulted anywhere.

Secondary problem surfaced during this design (and folded in because its
first consumers are this feature's degradation paths): **component errors are
invisible.** CodeGraph's fail-silent fallback, callback failures, and every
other caught-and-degraded error exist only as container log lines — nothing
persistent, nothing in the TUI. A quietly-degrading system looks healthy.

The operator will clone `odoo/odoo`, `odoo/enterprise`, and
`odoo/documentation` (all branches) to the server; REVA consumes them
read-only.

## Context (verified against code + measurements, 2026-07-04)

- **Scale (19.0, measured locally):** core = 47,714 files / ~1.2 GB tree, of
  which ~862 MB is `.po`/`.pot` translations (code+XML ≈ 291 MB); 625 addons.
  Enterprise = 755 addons, ~678 MB (~172 MB w/o translations).
  `odoo/documentation` = RST/Sphinx, version branches matching core; **1,137
  RST files ≈ 9 MB text** (the other ~630 MB is screenshots).
- **Structure:** highly declarative — 1,472 `_name`, 2,767 `_inherit`, ~16k
  field definitions, per-module manifests/ACL CSVs. Odoo's own official LSP
  (`odoo-ls`) indexes exactly these deterministically. Prior art:
  odoo-code-search.com (`model:`/`field:`/`view:` keyword search).
- **CodeGraph (existing engine):** indexed 464 files in 4 s — no data at
  47k-file scale; index rebuilt every PR-review run (`git clean -fdx` deletes
  `.codegraph/`); MCP server serves one directory (cwd) as coded; the roadmap
  already dropped a feature because CodeGraph 0.9.8 lacks field-schema data
  (`FEATURE_ROADMAP.md:118-120`). Its transferable lesson: **steering is
  mandatory** (spike: −26% cost only when the prompt points at the tools).
- **Review path:** CLI runs with `cwd=<clone>`, `--allowedTools
  Read,Grep,Glob,Write`, **no `--add-dir` anywhere**; `review_guidance.md:34-40`
  already permits reading vendored `odoo/` for context but forbids findings on
  it; `_ground_findings` drops findings citing files outside the clone;
  `_redact_internal_paths` scrubs `/repos|/tmp|/home|/app` from PR text.
  Optional skill params are added only-when-present (prompt-cache friendly);
  `manifest_audit` is the existing deterministic-hints pattern;
  `stated_intent` the existing fenced-reference pattern.
- **Ticket path:** single Messages call, no tools, no retrieval;
  `_build_system()` returns a list of cache-controlled system blocks — the
  clean injection seam (`reva/ticket_analyzer.py:99-109`);
  `ClaudeClient.review` supports multiple system blocks but no tool loop.
- **Infra traps:** anything under `/repos/{owner}/{name}` is rmtree'd by the
  30-day eviction sweep; the worker entrypoint `chown -R`s the whole `/repos`
  volume every boot. Egress allowlist already covers `github.com`.
- **Nothing overlaps in the roadmap:** intent-grounded review (shipped) feeds
  GitHub-issue text, not core knowledge; "RAG over prior findings" (Tier 3 F)
  is about finding-similarity, not code/docs; the learned-memory feature
  (shipped 2026-07-03, `7d794e1`) injects team preferences, not core facts.

### Locked decisions

1. **Approach A — local worktrees + structured registry. No vector DB, no
   graph DB, no new database technology.** Rationale: core knowledge is
   exact-name structured (registry beats embeddings); docs text is ~9 MB
   (Postgres FTS suffices); the CLI reads local files for free (the cost
   lever is steering, not retrieval); pgvector would add an embedding-vendor
   dependency REVA doesn't have. **Documented upgrade trigger:** if
   German-ticket retrieval hit-rate on a golden set proves weak despite the
   query planner, swap the FTS query helper for pgvector + multilingual
   embeddings — schema and seams unchanged.
2. **Version-flexible:** operator clones carry all branches; REVA loads the
   versions listed in `REVA_CORE_VERSIONS` (17.0/18.0/19.0 now; layout
   supports more).
3. **Review output:** new advisory finding category `standard-functionality`,
   default severity minor/medium, never blocking, must cite the customer's
   file.
4. **Ticket output:** new dedicated structured result field
   `standard_coverage`, rendered as its own HTML section.
5. **Docs:** local clone of `odoo/documentation` (no web access at
   review/ticket time).
6. **Fail-loud at startup, log-and-degrade at runtime:** when the feature is
   enabled, missing worktrees/catalog/registry for any configured version
   refuse worker boot. Runtime failures never fail a review/ticket but are
   always logged **and recorded as ops events**.
7. **Ops-event log is part of this spec:** persistent `ops_events` table +
   TUI visibility, retrofitted onto existing silent degradations (CodeGraph
   first). New invariant for CLAUDE.md: *any caught-and-degraded error must
   both log and record an ops event.*
8. **CodeGraph is untouched** — customer-repo-only, exactly as today.

### Explicitly out of scope (v1)

- pgvector / embeddings (trigger above), CodeGraph over core, agentic
  tool-loop in `ClaudeClient`.
- REVA fetching/updating the core clones itself (operator + cron own
  `core_sync.sh`).
- OCA repos as knowledge sources; versions below 17.0 (layout supports them,
  nobody loads them).
- Blocking behavior for `standard-functionality` findings.
- Ops-event alert thresholds (Google Chat push alerting stays as-is; this is
  the persistent browsable layer underneath).

## Design

### 1. Storage layout, sync, and mounts

```
/core/                          ← host dir (e.g. /srv/reva-core), BIND-mounted ro into the worker ONLY
  19.0/
    odoo/                       ← git worktree (sparse: excludes **/i18n/, *.po, *.pot)
    enterprise/                 ← same
    documentation/              ← worktree (sparse: content/ only — no images, no locale/)
    catalog/                    ← generated greppable knowledge pack (§2)
  18.0/ …   17.0/ …
```

- `scripts/core_sync.sh <version…>` runs **on the host**: for each version,
  `git worktree add` (or fetch+reset update) from the operator clones with
  sparse-checkout rules, writing into the host dir; then triggers the
  extractor + Postgres load **inside the worker container** via
  `docker compose exec worker python -m reva.odoo_registry load …` (the
  worker sees `/core` read-only and already has DB access — the load only
  reads files and writes Postgres). Operator-run or cron; REVA never fetches
  core itself.
- Sparse checkout cuts each version from ~2.1 GB to **≈500 MB**.
- Read-only mount = filesystem-level write protection (stronger than CLI
  flag semantics); outside `/repos`, so the eviction sweep and the boot-time
  `chown -R` never touch it.

### 2. Registry extractor, catalog, docs FTS (`reva/odoo_registry.py`)

Deterministic extractor, run **offline** by `core_sync.sh` (never at
review/ticket time), CLI entry `python -m reva.odoo_registry load
/core/<ver> --version <ver>` (idempotent per-version replace):

- **Parses:** model definitions (`_name`/`_inherit`/`_inherits` via Python
  AST, not regex), field definitions (name, type, string, compute/related),
  `__manifest__.py` (module, depends, category, summary), view/action/menu
  XML ids, `ir.model.access.csv` rows. Per-file parse errors are skipped and
  counted in the load summary — never abort a load.
- **Output 1 — Postgres tables** (keyed by `odoo_version`):
  `odoo_core_modules` (~1.4k rows/ver), `odoo_core_models` (~2.5k),
  `odoo_core_fields` (~26k), `odoo_docs_sections` (~10k sections: RST split
  on headings — title, path, anchor, body ≤ ~2k chars). Total across three
  versions: well under 500 MB with indexes.
  - **FTS as raw-SQL GIN expression indexes in the migration** (Postgres-only
    DDL). The query helper does `to_tsvector(...) @@ plainto_tsquery(...)` on
    Postgres and a degraded `LIKE` fallback on SQLite — matching the repo's
    unit-test (SQLite) vs `make test-integration` (Postgres) convention.
- **Output 2 — plaintext catalog** at `/core/<ver>/catalog/`: one file per
  module (`sale.md`: models with key fields, views summary, depends, doc
  cross-references). This is what the CLI greps — "does something like X
  exist" becomes a one-grep answer instead of a 47k-file exploration.
- Shared read-side helper module `reva/core_knowledge.py`: version
  resolution, `search_docs(version, terms)`, `search_registry(version,
  terms)`, `core_paths(version)` — the single seam both paths consume.

### 3. Review-path integration

- **full / deep / audit** (repo-aware skills only): the CLI invocation gains
  `--add-dir /core/<ver>/odoo --add-dir /core/<ver>/enterprise --add-dir
  /core/<ver>/documentation`, plus a fenced `core_knowledge` preamble note:
  *catalog first, source second, docs for functional questions* — steering
  is mandatory (CodeGraph lesson). New `review_guidance.md` section + skill
  notes define the **`standard-functionality`** category: advisory
  (minor/medium), cites the **customer's file** (the existing
  out-of-clone grounding drop stays as-is), body names the stock
  feature/module + docs reference. Core paths in finding bodies are written
  module-relative (`addons/sale/...`); `/core` is additionally appended to
  `_redact_internal_paths`' scrub list as a belt-and-braces.
- **diff / delta / xml** (cost-sensitive): stay off `--add-dir`. New
  deterministic `core_overlap` skill param (the `manifest_audit` pattern):
  registry lookup over the diff's added models/fields — custom field
  duplicating an existing core field on the same model, custom model name
  closely matching a core model — at most ~10 compact hints; empty → param
  omitted (prompt-cache friendly). The model verdicts hints; hints are
  advisory input, not findings by themselves.
- Version from `.claude-review.yml` (`odoo_version: "19.0"`). Repo without a
  version, or version not loaded → no core params/add-dirs; **runtime
  degradation logged + ops event** (config drift is visible, run proceeds).

### 4. Ticket-path integration

`ticket_runner` gains one pre-step; the analysis remains a single call:

1. **Query planner:** one Haiku call (`REVA_VERIFY_MODEL`), fenced ticket
   text in, forced tool out: English search terms + candidate Odoo
   app/module names + a `worth_checking` flag (skip retrieval for pure
   process tickets). ~$0.002, ~2–4 s; cost recorded in the spend ledger
   (kind `"ticket_planner"`).
2. **Retrieval:** `core_knowledge.search_docs` + `search_registry` for the
   instance's version; top ~8 results deduped, ~4–6k tokens.
3. **Analysis call:** retrieved context appended as an extra
   **cache-controlled system block**; `prompts/ticket_analysis.md` gains the
   `standard_coverage` section instructions with a scoped carve-out to the
   "no technical details" rule: this section may name Odoo apps/settings/
   features (consultant-level); code-level detail stays banned elsewhere.
   `odoo_notes` unchanged.
4. **New result field** on `TicketAnalysisResult`:
   `standard_coverage: {coverage: "full"|"partial"|"none"|"unknown",
   features: [{name, module, kind: "app"|"setting"|"feature", how,
   reference, confidence}], notes}` — rendered as `<h2>Standard Odoo
   Coverage</h2>` in the HTML (`ticket_formatter`).
5. Planner/retrieval failure → analysis proceeds without the block,
   `coverage: "unknown"`, error logged + ops event.
- Version from new nullable `odoo_instances.odoo_version` column (PATCH +
  create accept it; admin-audited). Instance without a version → same
  degradation semantics as reviews.

### 5. Configuration & startup validation (fail-loud)

- `REVA_CORE_KNOWLEDGE_ENABLED` (default false — everything runs exactly as
  today), `REVA_CORE_KNOWLEDGE_DIR` (default `/core`),
  `REVA_CORE_VERSIONS` (comma list, e.g. `17.0,18.0,19.0`).
- **When enabled, worker startup validates every listed version**: the three
  worktrees exist under `/core/<ver>/`, `catalog/` is non-empty, and
  registry rows exist in Postgres for that version. Any failure →
  `RuntimeError` at boot; the worker refuses to start (the
  `Settings.from_env` fail-loud convention). Misconfiguration is a deploy
  failure, never a silent degradation.
- All new env vars documented in `.env.example` (the C10 drift test from the
  hardening batch will enforce this once both land).

### 6. Ops-event log (cross-cutting)

- **New table `ops_events`**: `id`, `component` (`codegraph`,
  `core_knowledge`, `ticket_planner`, `retrieval`, `odoo_callback`,
  `git`, …), `severity` (`warning`|`error`), `event` (short slug),
  `detail` JSONB (repo, run/analysis id, version, exception text),
  `created_at` (indexed). Writer
  `writers.record_ops_event(db, component, severity, event, detail)` —
  wrapped so a failed ops-write can never break the operation it observes.
- **Hook points (v1):** `_codegraph_prepare`'s fallback branch (retrofit —
  today's worst silent degradation), every core-knowledge degradation in
  §§3–4, ticket planner/retrieval failures, Odoo callback
  Transient/Permanent errors, git-op retries in `claude_code_runner`.
- **New CLAUDE.md invariant:** any caught-and-degraded error must both log
  and record an ops event.
- **API:** `GET /api/v1/ops-events?component=&severity=&limit=` (master
  key). **TUI:** Failures tab (4) gains a toggleable second view ("failed
  runs" ⇄ "component events"); dashboard gains a "degradations last 24h"
  counter and a `core_knowledge: {versions, loaded_at}` status line
  (`/api/v1/metrics/dashboard`).
- **Retention:** daily purge, `REVA_OPS_EVENTS_RETENTION_DAYS` default 30,
  in the existing retention pass.

### 7. TUI summary (principle 5)

Tickets tab: `standard_coverage.coverage` badge column on analyses. Odoo
tab: instance `odoo_version` column. Dashboard: core-knowledge status +
degradation counter. Failures tab: ops-events view. All via existing
tab/client/mock patterns; `go build/vet/test ./...` stays green.

## Error handling summary

| Case | Behavior |
|---|---|
| Feature enabled + missing worktree/catalog/registry for a configured version | **worker refuses to boot** (RuntimeError at startup) |
| Feature disabled (`REVA_CORE_KNOWLEDGE_ENABLED` unset/false) | zero behavior change anywhere |
| Review/ticket references a version not loaded (config drift) | run proceeds without core knowledge; error log + `ops_events` row |
| Haiku planner fails/times out | analysis proceeds, `coverage: "unknown"`; log + ops event |
| Retrieval/FTS error | same degradation; log + ops event |
| Extractor per-file parse error | skip + count in load summary (summary logged; errors > threshold visible in the load output) |
| Catalog grep finds nothing at review time | model behavior, not an error — the skill says "absence of catalog hits is weak evidence, verify in source" |
| `record_ops_event` itself fails | swallowed + logged; never propagates |
| Stale core knowledge (core moved on since last sync) | acceptable — advisory-only outputs; `core_sync` cadence is the freshness knob |

## Testing

- **Extractor:** fixture mini-addons in `worker/tests/fixtures/core/`
  (model/field/manifest/view/CSV parsing, `_inherit` chains, parse-error
  skip counting).
- **FTS helper:** dialect split — LIKE fallback unit-tested on SQLite; real
  `tsvector` behavior via `make test-integration` (expression indexes are
  raw-SQL-migration-only, per repo convention).
- **`core_overlap`:** hint generation on fixture diffs (duplicate field on
  core model, near-name model, no-hint case → param omitted).
- **Review wiring:** CLI arg assertions — `--add-dir` present only for
  repo-aware skills with a resolved+loaded version; preamble contains the
  fenced steering note; category appears in guidance.
- **Ticket wiring:** planner mocked (forced-tool schema), retrieval seeded,
  system-block order + cache markers + fencing asserted; `standard_coverage`
  schema/formatter round-trip; degradation paths (planner failure →
  `unknown` + ops event).
- **Startup validation:** boot-refusal matrix (missing worktree / empty
  catalog / no registry rows / disabled flag).
- **Ops events:** writer safety (failing DB write swallowed), hook-point
  tests (CodeGraph fallback records an event), API filters, retention purge,
  TUI mock rendering.
- **Staging live-gates (A1/A2 pattern):** (1) one full review with core
  mounted — confirm `--add-dir` reads work with pinned CLI 2.1.160 and a
  real `standard-functionality` finding cites the customer file; (2) one
  German ticket end-to-end — planner terms sensible, retrieval non-empty,
  coverage section renders; (3) `core_sync.sh` full run on the server —
  measure extractor duration + table sizes.

## Open questions

- `--add-dir` semantics under the pinned CLI (2.1.160) are asserted by the
  staging gate, not assumed — if reads through `--add-dir` misbehave, the
  fallback is bind-mounting core *into* the clone path at review time
  (symlink/bind into cwd), which the design otherwise avoids.
- Golden-ticket set (~10 real German tickets) for measuring retrieval
  hit-rate — the pgvector upgrade trigger. Assembled during staging, not
  part of the implementation plan.
- Migration numbers: three pending plans already contend for 025/026 —
  take the next free numbers at implementation time.
