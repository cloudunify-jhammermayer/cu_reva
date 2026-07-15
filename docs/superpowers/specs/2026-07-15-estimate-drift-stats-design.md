# Estimate Drift Stats — C1 Second Half (Display-Only)

**Status: 📋 DESIGN — ready for a plan.** Companion to
`2026-07-15-ticket-actuals-design.md` (the shipped actuals endpoint). Output
shape locked 2026-07-15: **display-only** — REVA shows the drift; Joseph
adjusts the bands in `prompts/ticket_analysis.md` by hand (the standing
calibration rule: bands anchor to Cloudunify actuals). Band-edit suggestions
and auto-PRs are explicit non-goals until asked for.

## What it answers

"Are the ticket-analysis estimate bands still honest?" — per completed
ticket, planned hours vs. hours actually booked.

## Data + join (no new tables, no GitHub calls)

- **Actuals:** `ticket_actuals` (migration 040) — one row per (instance,
  ticket, model), pushed by Odoo on ticket-done.
- **Estimates:** the ticket's issue union (`writers.get_ticket_issue_union`)
  summing `estimate_hours` — the same source the journey's "Xh estimated"
  line uses. This copy is fresh for REVA-created estimates and Odoo-side
  edits (`update_ticket_issue_estimate` rewrites the JSON across all runs);
  estimates edited directly on the GitHub board are invisible here —
  documented gap, acceptable for a display-only view.
- **Ratio:** `actual_hours / estimate_total`, computed only when
  `estimate_total > 0` and the ticket has actuals. Tickets missing either
  side are excluded (and counted, so the view can say "12 done tickets, 9
  comparable").

Granularity is **per ticket**, not per band: actuals arrive as ticket totals,
so the prompt's per-story bands (configuration / small / medium / large)
can't be scored individually. Proxy grouping instead, by estimate total:
**S < 8 h, M 8–20 h, L > 20 h** — enough to see "small tickets overshoot,
large ones don't".

## Surfaces

1. **`GET /api/v1/metrics/calibration`** (master gate, mirrors
   `/metrics/learning`): query `calibration_stats(db, since_days=180)` in
   `api/app/queries/metrics.py` — JSON-in-Python join, portable to SQLite
   like the journey query. Returns per-ticket rows
   `{odoo_instance_id, ticket_id, model_name, estimate_hours, actual_hours,
   ratio, reported_at}` plus a summary
   `{done_tickets, comparable, median_ratio, p25, p75, by_size: {S,M,L}}`.
2. **TUI:** an "Estimate calibration" block on the Feedback tab
   (`tui/internal/ui/feedback.go` — it already frames itself as the
   learning-signals view): summary line + worst 3 tickets. Client/iface/mock
   /demo additions per the existing tab pattern.
3. **Value report:** one calibration paragraph in the monthly report body
   (median ratio, comparable count, size-bucket medians). Chat notification
   behavior unchanged.

## Non-goals

- No suggested band values, no auto-PR edits to `prompts/ticket_analysis.md`,
  no alarms/ops-events on drift. Display only.
- No live board reads at query time (the API never calls GitHub).

## Testing

- SQLite unit tests for `calibration_stats`: happy join, zero-estimate
  exclusion, multi-run union dedup, instance scoping, size buckets.
- TUI: `go test ./...` golden additions for the Feedback tab block; demo
  mode shows plausible fake stats.
- Value report: existing runner tests extended with an actuals fixture.
