# Session TLDR: Completed REVA Plans

Implemented and committed REVA-side work for:

- Odoo Core Knowledge
- Timesheet Wording Review
- Odoo/REVA Contract Tests
- Ticket PR Loop Closure
- Monthly Value Reports

## Odoo Core Knowledge

- Added operator-managed Odoo core knowledge support.
- REVA can load synced Odoo core, enterprise, and documentation trees by Odoo version.
- Reviews and ticket analysis can use the configured Odoo version for better standard-module context.
- Added API/TUI visibility for loaded core knowledge status.
- Added setup/sync support around the `scripts/core_sync.sh` operator flow.

## Timesheet Wording Review

- Added the REVA-side timesheet wording review flow.
- Odoo can submit timesheet lines for wording checks.
- REVA returns reviewed line status, rewritten descriptions, reasons, and summary stats.
- Callback path was corrected to `/hr/timesheet-results`.
- Added validation for line roles and bounded flagged-word handling.

## Odoo/REVA Contract Tests

- Added generated Odoo contract schemas and samples as a shared compatibility surface.
- Added tests to ensure inbound request contracts are covered by samples.
- Added drift checks so generated contracts stay in sync with runtime schemas.
- Added `contracts/manifest.json` with a `contracts_version` pin for the Odoo addon.
- Added sync workflow expectation for AST/Odoo via `scripts/sync_contracts.sh`.

## Ticket PR Loop Closure

- Ticket analyses now persist structured results for later review grounding.
- PR closing refs such as `Closes #123` can resolve back to REVA-created Odoo ticket issues.
- PR reviews can include linked Odoo ticket acceptance criteria as prompt context.
- Create-issues accepts optional `github_username` and assigns created GitHub issues when possible.
- If GitHub rejects an assignee, REVA retries without the assignee and logs an ops event.
- REVA sends `/tickets/ready` to Odoo when all linked GitHub issues are closed.
- REVA sends `/tickets/change-note` to Odoo after a merged PR closes a REVA-created issue.
- Added `ticket_grounding: false` and `change_notes: false` repo kill switches.
- TUI/API/weekly report now surface ready tickets.
- New Odoo callback contracts were generated for ready and change-note callbacks.

## Monthly Value Reports

- Added persisted monthly value reports in REVA.
- Scheduler enqueues one report per month for the previous calendar month.
- Worker builds reports from DB rollups only; no Claude call.
- Reports are always persisted.
- Optional Google Chat delivery is default-off via `REVA_VALUE_REPORT_CHAT_ENABLED=false`.
- Added API endpoints:
  - `GET /api/v1/value-reports`
  - `GET /api/v1/value-reports/latest`

## Odoo Follow-Up

The Odoo/AST side still needs to consume the new contracts:

- Send optional `github_username` on create-issues.
- Implement `/api/reva/tickets/ready`.
- Implement `/api/reva/tickets/change-note`.
- Sync `contracts/` and update the addon contract version pin.
- Implement the Odoo receiver for timesheet wording review at `/hr/timesheet-results`
  if not already present on the AST side.

See:

- `docs/ast-odoo-ticket-loop-and-value-report-guide.md`
- `contracts/manifest.json`
