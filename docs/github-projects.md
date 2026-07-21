# GitHub Projects (v2) board for ticket issues

Optional extension of the [GitHub issue creation](github-issue-creation.md)
flow. When an Odoo `create-issues` request carries a **GitHub Project board
URL** (and, optionally, a **plan date**), REVA adds every issue it creates —
each child issue **and** the parent epic — to that Projects v2 board and stamps
its fields. Absent those inputs, issue creation behaves exactly as before.

Implemented 2026-07-09 (spec `docs/superpowers/specs/archive/2026-07-09-github-projects-plan-date-design.md`).
The Odoo side (`cu_reva_ticket_analysis`) already sends `github_project_url` +
`plan_date` and renders the per-issue **Planned Date** / **Completed Date**
columns REVA echoes back.

## GitHub App permission (required, one-time)

Projects v2 has **no REST API** — REVA drives it over GraphQL, which needs the
org-level **Projects: Read & write** permission on REVA's GitHub App. Add it in
the App settings; **every installation must then re-approve** the new
permission before board writes work.

Until an installation approves, board projection **fails soft**: issues are
still created and reported to Odoo normally, and each failure is recorded as an
ops event (`project_step_failed`, source `github`) visible on
`GET /api/v1/ops-events` and the TUI **Failures** tab. The board is a bonus, not
a gate — nothing about issue creation breaks while the permission is pending.

## Fields REVA manages

Resolved once per run against the target board. REVA **never rewrites existing
single-select options** — a mismatch is skipped and recorded as
`project_field_unmatched` (source `github`), not "fixed". The one field it
renames is the date field: a matched legacy `Plan date` (or `due date`) is
renamed to `Due date` in place, which **preserves its existing values** (a
best-effort rename — if it fails REVA still uses the field under its old name).

| Field | Behavior |
|---|---|
| **Due date** (date) | Reuses an existing custom DATE field named `Due date`; a legacy `Plan date`/`due date` field is renamed to `Due date` in place (values preserved). If neither exists **and** the request carried a date, REVA creates a `Due date` field. The built-in roadmap `Start date`/`Target date` are issue-backed and deliberately not used. Set on every added item (including the epic). Skipped when the request sent no date. |
| **Estimate** (number) | Reuses an existing `Estimate` NUMBER field, else creates one. Set to the planned hours on each added item that carries an estimate; skipped for items without one. Updated later when the estimate is edited in Odoo's issue table (`POST /api/v1/update-issue-estimate` → `update_issue_estimate` job) — the board is the only GitHub surface for estimates; issue bodies never render them. |
| **Status** (single-select) | Sets the built-in `Status` to its `Todo` option **only when first adding an item**. If the board has no `Todo` option, Status is left unset (`project_field_unmatched`). |
| **Priority** (single-select) | Reuses a `Priority` single-select, else creates one with options **Low / Medium / High / Urgent**. Mapped from the Odoo priority key: `0`→Low, `1`→Medium, `2`→High, `3`→Urgent (unknown → Medium). If a `Priority` field exists but lacks the mapped option name, it is skipped (`project_field_unmatched`). |

Fields are set **only when an item is first added to the board** — tracked by a
persisted per-item project item id. A card a developer later moves out of
`Todo` (or re-dates) is never reset by a re-run or a later request for the same
ticket.

## Recommended board configuration (no code — project settings)

REVA only adds items and sets the fields above; the board's own automations own
everything after that. Recommended one-time setup on the board:

- **Built-in workflows** (project → ⚙ → Workflows): enable *Item closed →
  Status: Done* and *Pull request merged → Status: Done*. Combined with REVA's
  `Todo`-on-add, the board tracks reality with zero further API writes.
- **Roadmap view:** set its date field to **Due date** for a timeline of all
  Odoo-planned work.
- **Board/table view grouped by Parent issue:** one swimlane per Odoo ticket,
  each epic showing its sub-issue progress. Slice further by the **Priority**
  field or the per-type labels (`BUG`, `FEAT`, `CR`, …).

## Odoo side

The board URL is a single **global** setting (Settings → Cloudunify → REVA
Ticket Analysis → **GitHub Project Board**) sent with every request; the plan
date comes from the record's **Planned Date** (or the Create Feedback wizard).
Both are optional. Re-clicking **Create Issues** on an already-issued ticket
after configuring the board **backfills** its existing issues onto it (REVA
fetches their node ids and adds them; no new issues are created).

Per-issue dates flow back on the issues-created / issue-state callbacks:
`plan_date` (the request's date, echoed onto each issue) and `complete_date`
(the UTC date an issue was closed, cleared on reopen), both plain `YYYY-MM-DD`.
