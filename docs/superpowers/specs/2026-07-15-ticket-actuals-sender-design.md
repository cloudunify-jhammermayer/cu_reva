# Ticket-Actuals Sender — ast-odoo Side

**Status: 📋 DESIGN — ready for a plan (implements in ast-odoo, rides the
connector's next wave).** Counterpart to the shipped REVA endpoint
(`2026-07-15-ticket-actuals-design.md`); contract `ticket-actuals` is already
in `reva_contracts/` with the test pin bumped to `8fc96171…`.

## Behavior

When a ticket/task with REVA activity is marked **done** (the manual
deployment step — see the standing no-auto-done rule; REVA never closes
tickets, people do), the addon POSTs the record's timesheet totals to
`POST /api/v1/ticket-actuals`:

```json
{"ticket_id": 42, "model_name": "helpdesk.ticket",
 "actual_hours": 7.5, "timesheet_line_count": 4}
```

- **Fire-and-forget.** Wrap the send in a broad try/except that logs (the
  `reva.request.log` mixin already records attempts) and **never raises** —
  a REVA outage must not block marking a ticket done. This is the opposite
  of `update-issue-estimate`, which blocks by design; don't copy its
  error handling.
- **Re-done tickets re-send** on every done transition — the endpoint is
  latest-wins, so no client-side dedup.
- **Filter to REVA tickets.** Only send when the record has REVA activity
  (the existing computed indicators on the extended models — analysis or
  issue runs). The endpoint would accept anything from an authenticated
  instance, but pushing every done ticket org-wide is noise.

## Where it hooks

`cu_reva_ticket_analysis` already extends both models
(`models/helpdesk_ticket.py`, `models/project_task.py`). Override `write()`
and trigger on the **transition into done** (not on every write while done):

- `helpdesk.ticket`: the write newly sets the closed indicator
  (`close_date` transitioning from unset, i.e. moving into a closing stage).
- `project.task`: the equivalent done indicator on the instance's Odoo
  version (`state == '1_done'` on 17+; verify against the target instance
  at implementation).

Totals: sum `unit_amount` over the record's `account.analytic.line`
timesheet lines (the ticket's own lines for helpdesk with timesheets
enabled; `task_id` lines for tasks); `timesheet_line_count` = number of
lines. Zero lines still sends `actual_hours: 0` — "done with no time
booked" is itself a calibration signal.

Transport: the existing `reva.connector.mixin._reva_post` (Bearer instance
key + optional CF Access service-token headers — both already implemented).

## Testing

- Addon unit tests with the HTTP call mocked: fires once on the done
  transition, not on unrelated writes; sums lines correctly; a raised
  request exception is swallowed and logged; no send for records without
  REVA activity.
- Contract: the pinned `reva_contracts` tests already validate the payload
  shape against `ticket-actuals.schema.json`.
- Staging gate (joint, tracked in `docs/ops-debt-runbook-2026-07.md`): mark
  one real staged ticket done → REVA row exists, journey shows
  `actuals_reported`.

## Non-goals

- No batch backfill of historical done tickets (can be a one-off script
  later if calibration needs more data).
- No retry queue; a missed push self-heals on the next done transition or
  a manual re-done toggle.
