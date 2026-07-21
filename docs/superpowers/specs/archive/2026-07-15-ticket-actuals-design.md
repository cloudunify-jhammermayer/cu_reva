# Ticket Actuals — Odoo Pushes Timesheet Totals on Ticket-Done

**Status: ✅ IMPLEMENTED (2026-07-15, same-day). REVA side complete; ast-odoo
sender owed (rides the connector's next wave).**

The actuals half of the estimate-calibration loop (C1, deferred from
`2026-07-10-issue-estimates-to-odoo-design.md`). Estimates live only on the
Projects board; REVA had no record of hours actually spent — the
timesheet-wording review persists line ids and verdicts, never hours.

## Decisions (locked with Joseph 2026-07-15)

- **Push, not pull.** The `cu_reva_connector` addon POSTs per-ticket timesheet
  totals to REVA when a ticket is marked done — the manual deployment step,
  which is also the moment actuals are final. REVA never calls into Odoo.
- **Per-ticket totals**, not per-line: consultants book time per task/ticket,
  and calibration compares ticket actuals against the sum of its issues'
  board estimates.
- **Latest wins.** One row per (instance, ticket, model); a re-done ticket
  re-sends its totals and the row is replaced (`reported_at` bumped).
- **No estimate snapshot at receive time.** Whether/where estimates are
  joined (drift stats, band suggestions) is the second half of C1 — output
  shape still to be decided (default proposal: display-only per-band error in
  TUI + monthly report). No board/GitHub calls on this path, so the endpoint
  is a synchronous DB upsert — no RQ job, no budget check.
- **Tickets without REVA issues are accepted** — the instance is
  authenticated; calibration simply has nothing to compare them against yet.

## Shape

- `POST /api/v1/ticket-actuals` (instance-key gate, contract
  `ticket-actuals`): `{ticket_id, model_name, actual_hours ≥ 0,
  timesheet_line_count?}` → `200 {"status": "recorded"}`.
- Migration `040_ticket_actuals.sql` / `reva/db/models.py::TicketActual`;
  writers `record_ticket_actuals` (upsert) + `get_ticket_actuals`.
- Journey surfacing: `actuals_reported` event ("Ticket done — 7.5h actual,
  4 lines") in `GET /api/v1/ticket-journeys`; the TUI renders unknown kinds
  via its default symbol — zero Go changes.
- Contracts regenerated (`8fc96171…`), synced to ast-odoo, pin bumped in
  `cu_reva_connector/tests/test_contracts.py` (uncommitted there by design).

## Owed

- ast-odoo sender: designed in `2026-07-15-ticket-actuals-sender-design.md`.
- C1 second half: designed in `2026-07-15-estimate-drift-stats-design.md`
  (display-only, locked 2026-07-15).
- Staging: one real done-ticket push once the addon side ships (tracked in
  `docs/ops-debt-runbook-2026-07.md`).
