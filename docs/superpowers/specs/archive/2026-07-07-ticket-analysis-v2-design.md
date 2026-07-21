# Ticket analysis v2 — delivery visibility, HTML guard, lean output, dev-time estimate

**Date:** 2026-07-07
**Component:** `cu_reva` — `reva/` (types, ticket tool/prompt/formatter, new `html_guard`), `worker/` ticket runner, migration 033, `GET /api/v1/ticket-analyses`, TUI Tickets tab.
**Status:** Design approved (Q&A with Joseph, 2026-07-07), pending implementation plan.

## Problem

Four gaps in the ticket-analysis flow, surfaced in production use:

1. **Callback failures are invisible.** `ticket_analyses.status` flips to
   `completed` *before* the Odoo `write_field` callback. When the callback
   fails, only an ops event exists — the Tickets tab shows a green
   "completed" for an analysis the consultant never received.
2. **Malformed HTML reached Odoo.** At least one analysis rendered broken in
   the Odoo HTML field. There is no well-formedness check between the
   formatter and the callback.
3. **Output is too heavy.** Consultants need the gaps (missing information)
   and the build-vs-configure verdict (standard Odoo coverage). Acceptance
   criteria, test cases, DoR and DoD add noise they don't use.
4. **No effort signal.** REVA does not estimate development time, so
   consultants estimate manually from the same text REVA already analysed.

### Locked decisions (Joseph, 2026-07-07)

1. **Sections kept:** Summary, Missing Information, Odoo-Specific Notes,
   Standard Odoo Coverage, plus the new Development Estimate. ACs, ALL test
   cases (including edge/error), DoR and DoD are removed outright.
2. **Estimate format:** hour **range** (min–max) with confidence level and
   explicit assumptions — never a bare point estimate.
3. **Estimate scope:** implementation + developer testing, performed by a
   **mid-level developer working AI-assisted**. Deployment, PM, and customer
   communication are out of scope for the number.
4. **Split per user story, produced in the analysis** (not the issue run):
   the analysis prompt splits the ticket into user stories using the same
   splitting rules as the issue planner. The later GitHub-issue split may
   differ slightly; that is accepted. Issue generation stays estimate-free.
5. **HTML guard repairs rather than fails:** a malformed render must not
   fail the job; repair it, deliver it, and record an ops event.

### Explicitly out of scope

- Estimates in GitHub issue bodies / the issue-planner prompt.
- Any Odoo↔REVA contract change — the callback payload stays a single HTML
  string, so no `contracts/` regeneration and no ast-odoo sync.
- Backfilling `callback_sent_at` for historical rows (all pre-existing
  completed rows will read "not in Odoo"; acceptable, they age out).
- Re-rendering stored HTML of old analyses to the lean format.

## Design

### 1. Delivery visibility

- **Migration `033_ticket_analyses_callback.sql`** (idempotent, follows the
  house conventions): `ADD COLUMN IF NOT EXISTS callback_sent_at
  timestamptz`, `ADD COLUMN IF NOT EXISTS callback_error text` on
  `ticket_analyses`; matching fields on the ORM model (tests build from the
  models — a missing model field means the column is invisible to tests).
- **Writers:** `record_ticket_analysis_callback_sent(db, analysis_id)`
  (sets `callback_sent_at = now()`, clears `callback_error`) and
  `record_ticket_analysis_callback_failed(db, analysis_id, error)` (sets
  `callback_error`, leaves `callback_sent_at` NULL).
- **Runner (`worker/worker/ticket_runner.py`):** after `odoo.write_field`
  succeeds → `callback_sent`. In the existing except branch → record
  `callback_failed` *in addition to* the existing ops event, then re-raise
  as today so RQ retries. The existing idempotent-resume path (completed row
  + stored HTML → skip Claude, go straight to callback) means a successful
  retry overwrites the failure with `callback_sent_at`.
- **API:** `/api/v1/ticket-analyses` rows gain `callback_sent_at` and
  `callback_error`.
- **TUI Tickets tab:** a completed analysis with `callback_sent_at == null`
  renders `completed ⚠ not in Odoo` (status cell) and, when present, a
  `callback error: …` extras line styled like the existing
  `analysis error:` line. `types.go` / `mock.go` extended accordingly.

### 2. HTML render guard

- New **`reva/html_guard.py`**, stdlib-only (`html.parser`):
  `ensure_renderable(html: str) -> tuple[str, bool]`. Checks tag
  balance/nesting over the tag set the formatter emits (`p`, `h2`, `h3`,
  `ul`, `li`, `strong`, `em`, `small`, `span`, `br`). Repairs by closing
  unclosed tags in stack order, dropping unmatched closers, and escaping
  stray `<`. Returns `(html_or_repaired, was_repaired)`.
- **Runner** calls it on every outgoing analysis HTML immediately before
  `write_field`. `was_repaired == True` → deliver the repaired HTML and
  `record_ops_event("ticket_analysis", "warning", "html_repaired", …)` —
  degradations must be visible (Failures tab), never silent.

### 3. Lean output

- **`reva/types.py`:** `TicketAnalysisResult` keeps `summary`,
  `missing_info`, `odoo_notes`, `standard_coverage` and gains `estimates`
  (§4). `acceptance_criteria`, `test_cases`, `definition_of_ready`,
  `definition_of_done` are removed, along with the then-orphaned
  `AcceptanceCriterion` and `TicketTestCase` models. `SourcedItem` stays
  (used by `odoo_notes`). Old persisted `result_structured` rows still parse
  — pydantic ignores unknown keys on read.
- **`reva/ticket_tool.py`:** `allowed` set and `required` list shrink to the
  kept fields (+ `estimates`).
- **`prompts/ticket_analysis.md`:** AC/test/DoR/DoD instructions removed;
  missing-info, Odoo-notes, and standard-coverage guidance kept; estimation
  section added (§4).
- **`reva/ticket_formatter.py`:** drops the AC, Test Cases, DoR, DoD
  renderers and their stats-banner entries; the banner keeps the gap count
  and gains the total estimate.

### 4. Development-time estimate

- **Types:**

  ```python
  class StoryEstimate(BaseModel):
      story: str                      # one-sentence user story
      kind: Literal["custom_dev", "configuration", "mixed"] = "custom_dev"
      min_hours: float
      max_hours: float
      confidence: Literal["high", "medium", "low"] = "medium"
      assumptions: list[str] = Field(default_factory=list)
  ```

  `TicketAnalysisResult.estimates: list[StoryEstimate]` (default empty,
  `_unwrap_json_list` before-validator like the other list fields).
- **Prompt:** split the ticket into user stories with the same rules the
  issue planner uses; per story estimate implementation + developer testing
  for a mid-level Odoo developer working AI-assisted; `configuration`
  stories carry the configuration effort and say so; every range states its
  assumptions; deployment/PM/communication excluded.
- **Formatter:** new "Development Estimate" section — per story one line
  (`story — 4–8 h`, kind + confidence badge, assumptions in small print) and
  a total line (`Σ min` – `Σ max` h). Stats banner shows `est. 12–20h`.
- **API:** computed `estimate_hours_min` / `estimate_hours_max` (sums over
  `result_structured.estimates`, null when absent) on
  `/api/v1/ticket-analyses`.
- **TUI:** detail pane line `est. 12–20h` when present.

## Testing

- `reva`/`worker`: html_guard unit tests (well-formed passthrough, unclosed
  tag, stray closer, stray `<`); formatter tests rewritten for the lean
  sections + estimate rendering; analyzer schema tests updated; runner tests
  for callback_sent / callback_failed / repaired-HTML ops event; writers
  tests for the two new writers.
- `api`: serializer tests for the new fields incl. estimate sums.
- `tui`: `go build ./... && go vet ./... && go test ./...` — status-cell and
  extras-line rendering tests extended.
- Migration 033 raw SQL is exercised only on real Postgres —
  `make test-integration` or first staging boot, per house rules.
