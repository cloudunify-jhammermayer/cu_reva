# Issue estimates on the Odoo ticket — design

**Date:** 2026-07-10
**Component:** `cu_reva` — Odoo callback contract. Touches: `reva/odoo_client.py`
(`_ISSUE_KEYS`), `reva/odoo_contracts.py` (issue-ref model + `IssuesCreatedPayload`,
CONTRACTS samples, `contracts/` regen), `worker/worker/ticket_issue_runner.py`
(total on the created callback), TUI (verify estimates visible on tickets detail).
Coordinated (separate work): ast-odoo `cu_reva_ticket_analysis` consumer.
**Status:** Approved (Joseph, 2026-07-10) — implemented; see
plans/2026-07-10-issue-estimates-to-odoo.md.
**Superseded in part (2026-07-13):** estimates are no longer rendered in issue/epic
bodies — the board's Estimate field (kept in sync with Odoo edits via
`POST /api/v1/update-issue-estimate`) is the only GitHub surface; see
`docs/github-issue-creation.md` → "Estimate sync (Odoo → board)".

## Problem

REVA computes a per-issue low-end dev estimate (`estimate_hours`) and an epic
total, and writes them to the GitHub issue body and the Projects board Estimate
field — but the Odoo callbacks send only
`_ISSUE_KEYS = ("number", "title", "url", "state", "plan_date", "complete_date")`.
The consultant who quotes the customer never sees effort on the ticket without
opening GitHub.

## Context (verified 2026-07-10)

- Persisted issue refs in `ticket_issue_runs.issues` JSON already carry
  `estimate_hours` (`ticket_issue_runner.py` persists it per item); the union
  snapshot builder (`get_ticket_issue_union`, `reva/db/writers.py`) rebuilds
  union items with a fixed key set, so it dropped `estimate_hours` until this
  plan's Task 2 added the key explicitly.
- `_project_items(items, _ISSUE_KEYS)` filters keys and **preserves
  optional-key omission** — items lacking a key simply omit it, so pre-rollout
  runs degrade cleanly.
- In production, snapshots flow through the union builder, which materializes
  `estimate_hours: None` for pre-rollout items — so on the wire this is an
  explicit `null`, not an omitted key; the omission semantics above apply
  only to the `_project_items` mechanism itself, and null vs. omitted are
  equivalent for the addon's `.get()` reads.
- The runner already sums child estimates for the epic body annotation
  (`total = sum(i.get("estimate_hours") or 0 ...)`).
- Precedent: `plan_date`/`complete_date` rode into the same snapshots as
  optional additive per-issue fields (spec 2026-07-09); the shipped-addon
  contract note allows optional additive fields.

## Design

1. **Per-issue estimate on every snapshot.** Add `"estimate_hours"` to
   `_ISSUE_KEYS`. All snapshot-carrying callbacks gain it automatically:
   `/tickets/issues-created`, `/tickets/issue-state`, `/tickets/ready`.
   Add the matching optional field (`estimate_hours: float | None = None`) to
   the shared issue-ref contract model in `reva/odoo_contracts.py`.
2. **Total on creation.** `issues_created` gains top-level
   `total_estimate_hours: float | None` — sum over union items that have an
   estimate (children only; the epic is `parent_issue`, never in the union —
   no double-count). `None` when no item carries an estimate (pre-rollout
   runs). Computed where the callback payload is assembled in
   `ticket_issue_runner.py`, reusing the existing summing idiom.
3. **Contracts.** Update the `create-issues` / `issue-state` / `ready`
   CONTRACTS samples; regenerate `contracts/`
   (`python -m reva.odoo_contracts generate`); the coverage drift test
   enforces the sample updates. Re-sync ast-odoo (CLAUDE.md requirement for
   any Odoo<->REVA contract change).
4. **ast-odoo consumer (coordinated, out of this repo).** New optional
   `issue_estimate_hours` on `reva.github.issue` + list column; record-level
   total. **Labeling requirement:** display as "Dev estimate (low-end,
   AI-assisted)" — these are calibration-anchored dev hours, not a customer
   quote (see the estimate-calibration guidance in
   `prompts/ticket_analysis.md`).
5. **TUI.** The tickets detail already renders per-issue data from
   `ticket_issue_runs`; verify estimates are shown there and add the column/
   total line if missing (keep-the-TUI-in-sync principle).

Optional by construction: no estimates → fields omitted/`None`; no new kill
switch needed.

## Error handling

| Case | Behavior |
|---|---|
| Pre-rollout run items without `estimate_hours` | Key omitted per item (existing `_project_items` semantics); total `None` |
| Mixed union (some runs estimated, some not) | Sum over estimated items only; unestimated items omit the key |
| Odoo addon not yet updated | Unknown-field-tolerant handler ignores the additive fields (shipped-addon contract note) |

## Testing

- Unit (worker): `_ISSUE_KEYS` includes `estimate_hours`; snapshot items carry
  it end-to-end through `issues_created`/`issue_state`/`tickets_ready` fakes;
  total computation (all/some/none estimated); pre-rollout item omission.
- Contracts: round-trip for the three updated payloads; coverage drift test
  green after sample updates.
- TUI: mock rendering if a column is added; `go build ./... && go vet ./...`.
- Staging gate: one create-issues cycle shows estimates + total on the Odoo
  ticket.

## Out of scope

- Estimate-vs-actuals calibration loop (C1 — separately deferred, not specced).
- Re-estimating or updating estimates after creation (snapshots re-send the
  stored value only).
- Any customer-facing presentation of the numbers (consultant-internal).
