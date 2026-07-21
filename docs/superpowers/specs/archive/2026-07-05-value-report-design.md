# Monthly value report — design

**Date:** 2026-07-05
**Component:** `cu_reva` — new monthly rollup job (scheduler trigger + worker builder), `value_reports` table, `GET /api/v1/value-reports`, optional Chat delivery (default OFF).
**Status:** Design approved (Q&A with Joseph, 2026-07-05), pending implementation plan.

## Problem

There is no periodic per-repo / per-customer rollup of what REVA actually
delivers (findings caught and confirmed fixed, spend, throughput). The weekly
Chat report is operational, not evaluative; leads steer on anecdotes.

### Locked decisions

1. **Silent by default (Joseph):** the report is generated and **persisted**
   every month regardless, but Chat delivery is gated by
   `REVA_VALUE_REPORT_CHAT_ENABLED` (default **false**) — Joseph reviews the
   persisted reports manually first and flips the flag when satisfied.
2. Read surface v1 = master-key API (`GET /api/v1/value-reports`, newest
   first, full markdown content per row). TUI rendering is **deliberately
   deferred** together with the quality dashboard (conscious waiver of the
   TUI-sync principle, revisit with that feature).
3. Content is built ONLY from data that already exists — no new collection.

### Explicitly out of scope

- Customer-facing formatting/branding (internal steering document first).
- TUI surface (see above), PDF export, per-customer delivery routing.

## Design

- **Cadence:** scheduler fires monthly (`REVA_VALUE_REPORT_DAY` default 1,
  `REVA_VALUE_REPORT_HOUR_UTC` default 7), the weekly-reporter pattern:
  scheduler enqueues `worker.value_report_tasks.run_value_report` for the
  previous calendar month.
- **Builder (worker, pure queries — no LLM call):** per repo and per Odoo
  instance over the period: reviews run (by mode), findings by severity,
  **confirmed-fixed rate** (outcome ledger `resolved_by_fix` / findings),
  dismissal rate, spend (run tables + `claude_spend` by kind), tickets
  analysed, issues created/closed, tickets marked ready. Rendered to
  markdown (sections: fleet summary → per repo → per instance → cost
  breakdown).
- **Persistence:** `value_reports` table (`id, period_start, period_end,
  content_md TEXT, stats JSONB, chat_sent BOOLEAN, created_at`; unique on
  `(period_start, period_end)` — re-runs replace). Migration: next free
  number.
- **Delivery:** when `REVA_VALUE_REPORT_CHAT_ENABLED=true`, post the summary
  section to Google Chat (existing notifier), full content stays in the DB;
  `chat_sent` records it. Flag off → generate + persist only.
- **API:** `GET /api/v1/value-reports?limit=` (master key) — list with
  content; `GET /api/v1/value-reports/latest` convenience.
- Failures: builder errors → job fails (RQ retry; pure-DB so effectively
  deterministic) + ops event; Chat failure → report still persisted, ops
  event, `chat_sent=false`.

## Testing

Builder against a seeded SQLite DB (period windowing, per-repo/per-instance
rollups, empty-month report still generated); scheduler due-date logic
(month boundary, once-per-period); replace-on-rerun; API list/latest; Chat
gating (flag off → notifier never called; flag on → summary posted,
`chat_sent=true`); failure paths + ops events.
