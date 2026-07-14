# Ticket analysis grounded in customer repo docs — Design

- **Date:** 2026-07-14
- **Status:** implemented 2026-07-14 (REVA side: migration 039, ORM models,
  `reva/repo_docs.py`, `ticket_knowledge` restructure, `existing_customizations`
  output, TUI/API surfacing). Unit + real-Postgres integration tested; live
  model-behavior validation on staging still owed. No contract/ast-odoo change.
- **Context:** requested by Joseph 2026-07-14. Ticket analysis sees only the ticket
  text plus the "Retrieved Odoo knowledge" block (official docs + core registry,
  `reva/ticket_knowledge.py`) — it knows nothing about the customer's **existing
  customizations**, even though every analysis request already carries the repo
  link (`TicketJobParams.github_url`, stamped since migration 038 but unused by
  the worker). All customer repos will carry per-addon markdown docs
  (`custom_addons/**/*.md`, same scope as the consultant docs browser). Index
  those docs section-level in Postgres so retrieval injects only relevant
  sections (recurring token savings vs whole files), and surface the result as a
  consultant-facing **"Existing Customizations"** section (dedicated section
  confirmed by Joseph over folding into Standard Odoo Coverage).

## Design

1. **Docs index (migration 039)**: `repo_doc_sections` (`id BIGSERIAL`,
   `repo_full_name` lowercased owner/repo, `path`, `anchor`, `title`, `body`,
   `created_at`; plain index on `repo_full_name`; Postgres-only GIN FTS index on
   `to_tsvector('english', title || ' ' || body)` — migration 028 pattern) and
   `repo_docs_sync` (`repo_full_name PK`, `tree_sha`, `synced_at`, `files`,
   `sections`, `truncated`). Plus `ticket_analyses.repo_docs_sections_used
   INTEGER` (NULL = never attempted / legacy / resume; 0 = attempted, nothing
   injected; N = sections injected). ORM models mirror all of it (tests build
   from models).
2. **Shared module `reva/repo_docs.py`**: scope constants (`DOC_EXTENSIONS`,
   `SCOPE_PREFIXES`, `EXCLUDED_BASENAMES`) and `in_scope()` lifted from
   `api/app/routes/docs.py`, which imports them back (one definition of "the
   repo's docs"). `split_markdown_sections()` — ATX headings, code-fence aware,
   preamble section titled with the file stem, 2000-char body cap, slug anchors
   (reuses `DocSection` from `reva/odoo_registry.py`).
3. **Lazy sync at analysis time** (`sync_repo_docs`, never raises):
   installation token from the URL (`get_repo_installation_id` pattern from
   `ticket_issue_runner`), `get_repo` → default branch, `get_tree` → the tree
   payload's own top-level `sha` is the staleness key (fresh case = 2 API calls,
   0 file fetches). Stale → per-repo `pg_try_advisory_xact_lock(0x52444F43
   /*"RDOC"*/, crc32_signed(repo_full_name))` (two-int form, keyspace disjoint
   from the budget lock; busy → skip refresh, search current index; SQLite
   no-op) → re-check under lock → fetch in-scope files (caps: 50 files, 100k
   chars/file) → sectionize → delete+reinsert per repo (`load_version` pattern)
   → upsert `repo_docs_sync`. **Never wipe a good index when files were listed
   but every fetch failed.** Degradations record ops events (`repo_docs`:
   `sync_failed`, `tree_truncated`, `files_capped`, `files_failed`).
4. **One planner call feeds both blocks**: `build_knowledge_block` restructured
   into `plan_core_queries()` + `build_core_block()` + `build_repo_docs_block()`
   + orchestrator `build_ticket_knowledge()` returning a `TicketKnowledge`
   dataclass (`blocks`, `planner_cost`, `planner_error`, `repo_docs_error`,
   `repo_docs_sections`). Planner runs when ≥1 source is available (core
   resolved OR `github_url` parseable) — one spend record, no double-charging.
   Planner `terms + modules` rank repo docs via `search_repo_docs()`
   (dual-dialect: FTS + ts_rank on Postgres, `ilike` on SQLite; limit 8) with
   **OR-of-terms semantics** — one `plainto_tsquery` per term, OR'd, ranked by
   `ts_rank` over the combined query. Deliberate deviation from
   `CoreKnowledge.search_docs` (single `plainto_tsquery` = AND of all terms):
   with up to 13 terms+modules a same-section AND near-never matches — verified
   on real Postgres during review. Core block first, repo block second (stable
   prompt order).
5. **Fencing**: unlike the operator-provisioned core block, repo docs are
   repo-authored content — the block is **nonce-fenced** with the
   data-not-instructions framing (SECU-5, same as ticket text), headed
   `## Retrieved project documentation (owner/repo)`.
6. **Output section**: `ExistingCustomizations` (`coverage:
   full/partial/none/unknown = "unknown"`, `features: list[CustomizationFeature]`
   — `name`, `addon`, `how`, `reference` (doc path#anchor), `confidence` —
   `notes`) added to `TicketAnalysisResult` with `default_factory` (old persisted
   `result_structured` blobs stay valid — `StandardCoverage` rollout pattern).
   Added to `allowed` + `required` in `reva/ticket_tool.py` (strict schema).
   Rendered after Standard Odoo Coverage in `reva/ticket_formatter.py`, same
   gating (`coverage != "unknown" or features`). Prompt gains section
   "### 4. Existing Customizations" (Development Estimate renumbers to 5):
   base ONLY on the project-docs block, unknown+empty when absent, name addons
   and documented features only — no models/fields/code; the consultant-level
   exemption extends to it.
7. **Surfacing** (TUI-in-sync principle): `repo_docs_sections_used` flows
   through `GET /api/v1/ticket-analyses` (query + schema) to
   `TicketAnalysisSummary` in `tui/internal/api/types.go` and a `repo docs:N`
   meta line in the Tickets tab extras (`tui/internal/ui/tickets.go`); demo mock
   gains a row with it set. Failures are visible via the existing ops-events →
   Failures tab.
8. **Fail-soft behavior**: every repo-docs failure (invalid URL, sync failure,
   search failure) degrades to core-only analysis with an ops event; lock-busy
   and no-hits are info-log only. Retrieval sits entirely inside the fresh-
   analysis branch of `run_ticket_analysis` — the resume path (persisted HTML)
   never re-syncs or re-pays; the `MalformedModelOutput` retry re-calls only the
   analyzer.

## Out of scope

- Grounding the ticket-**issues** planner in repo docs (same block would slot in
  later).
- Backing the consultant docs browser's full-text search with the new table
  (today it fetches up to 300 files from GitHub on a cold search — natural
  follow-up).
- Webhook-driven (push-event) sync; lazy sync is sufficient at ticket volumes.
- Backfilling `repo_docs_sections_used` or re-analyzing historical tickets.
- Bilingual FTS: English planner terms won't stem-match German doc bodies under
  the `'english'` regconfig; module/addon names (language-neutral) mitigate.
  Follow-up if hit rates are poor: `'simple'` regconfig or bilingual planner
  terms.
- No Odoo↔REVA contract change (`github_url` already in the contract; the
  analysis returns HTML) — no `contracts/` regen, no ast-odoo change.

## Testing

- **Unit (SQLite)**: sectionizer (heading levels, fences, preamble, caps,
  anchors); `in_scope`; sync via a fake-GitHub class (`api/tests/test_docs.py`
  `_FakeGitHub` pattern): fresh fast-path does zero file fetches, tree-sha
  change re-indexes (delete+reinsert), caps/truncation/installation-404 →
  ops events, all-fetches-fail leaves the index intact, no in-scope files
  empties it; `search_repo_docs` ilike fallback; reworked
  `test_ticket_knowledge.py` — single planner call feeds both blocks, per-source
  availability matrix; runner tests — column persisted, ops event on
  `repo_docs_error`, resume path leaves the column NULL; types/formatter tests
  clone `test_standard_coverage_types.py`.
- **Integration (`make test-integration`)**: migration applies; real FTS +
  ts_rank query; advisory-lock busy-skip (session A holds the repo key,
  `sync_repo_docs` in B returns `"busy"` without writing).
- **Gates**: `reva/` touched → `make test` (all three services) + ruff; `tui/`
  touched → `go build/vet/test ./...`.
- **Honestly not unit-covered**: whether the model fills
  `existing_customizations` well from a real docs block. Validate on staging
  with one real ticket against a repo with addon READMEs: new section appears in
  the Odoo HTML, `repo docs:N` in the TUI, `repo_doc_sections` populated, second
  analysis hits the `"fresh"` fast path, no unexpected ops events.

## Accepted risks (v1)

- Tree `truncated=true` (>100k entries) / >50 doc files: proceed with what came
  back + ops event (no silent cap).
- File contents fetched by branch name may post-date the tree SHA (benign
  snapshot race; self-heals on the next sync).
- HTTP fetches run inside the locked transaction — that is what makes
  skip-if-busy meaningful; bounded at ≤50 sequential fetches, acceptable at
  ticket volumes.
