# github_url on ticket analyses — early repo grouping — Design

- **Date:** 2026-07-12
- **Status:** approved (design), dispatched for implementation same day
- **Context:** requested by Joseph 2026-07-12. TUI Tickets tab groups rows by the
  create-issues run's `github_url` (`tui/internal/ui/tickets.go::repoKey`); tickets
  that only have an analysis sit in "(no repo yet)" until issues are created. The
  Odoo record already knows its repo (`reva_github_url` from the project) at
  analysis time — send it along so grouping is correct from the first step.

## Design

1. **Inbound**: optional `github_url: str | None = None` on `TicketAnalysisRequest`
   (`api/app/schemas/ticket_analyses.py`). Empty string → None (Odoo Selection/Char
   idiom, same validator pattern as `CreateIssuesRequest`). When present, format-
   validated via the existing `reva/github_urls.py` parser → 422 on garbage
   (fail-fast consistency with create-issues), but **no reachability check** — an
   uninstalled App or a typo'd-but-well-formed repo must not block a paid analysis.
2. **Persistence**: nullable `github_url` column on `ticket_analyses` (migration
   038 — verify next free number — idempotent `ADD COLUMN IF NOT EXISTS`, ORM model
   updated; tests build from models). Stamped at create time; requeue reuses the row
   (no change needed).
3. **API out**: `github_url` on `TicketAnalysisSummary` and `TicketAnalysisStatus`.
4. **TUI**: `repoKey` falls back to the analysis's `github_url` when the ticket has
   no issue run (issue-run URL still wins when both exist — it is the operative
   one). Mock/demo client gains an analysis-only ticket WITH a repo so demo mode
   shows early grouping.
5. **Contracts**: the inbound `ticket-analysis` schema regenerates from the request
   model (`reva/odoo_contracts.py::_inbound_models`) — regen + ast-odoo sync + pin
   bump ride the change.
6. **ast-odoo sender** (consumer-side, one-line-ish): include `github_url:
   record.reva_github_url or ""` in the analysis submission payload. Additive and
   order-independent: old addon → key absent → today's behavior; new addon against
   old REVA → Pydantic ignores the extra key.

## Out of scope

- Journey/linkage use of the analysis-time repo (future; column makes it possible).
- Backfilling historical analyses.
- Any dedup/adoption logic change in the TUI (`sameRepoURL` untouched).

## Testing

- API: accepted+persisted; empty string → NULL; malformed → 422; absent → NULL
  (backward compat); summary/status expose it.
- TUI: analysis-only row with URL groups under the repo; no-URL row stays in
  "(no repo yet)"; issue-run URL wins over a differing analysis URL.
- Contracts drift test green after regen.
- Migration SQL is Postgres-only territory (unit tests cover the ORM), as usual.
