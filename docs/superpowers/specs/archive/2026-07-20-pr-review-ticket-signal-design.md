# Ticket-level PR review signal to Odoo (no-linked-issue fallback) — Design

- **Date:** 2026-07-20
- **Status:** implemented 2026-07-20 (REVA side; ast-odoo controller + prod is_default flag pending)
- **Repos:** cu_reva (this spec) + ast-odoo consumer (controller change owed on their
  side; contract sync per the usual flow)
- **Context:** requested by Joseph 2026-07-20. The work-status leg
  (`worker/worker/board_status_runner.py`, spec 2026-07-11) already tells Odoo
  `in_progress`/`in_review` — but only per REVA-created issue, resolved through the
  PR's closing-issue refs. A PR with **no linked issue** (dev skipped the issue-request
  flow, or linked hand-made issues) produces no Odoo signal at all.

## Problem

When a developer opens a PR without linking a REVA-created issue, the Odoo ticket the
work belongs to never hears that a PR exists or that REVA reviewed it. Ticket info
normally arrives via the issue request; when it didn't, REVA should extract the ticket
ID from the PR itself and still send the signal — to the Odoo instance the ticket
belongs to (single instance today: Cloudunify Prod, as default).

## Decisions (locked, from the 2026-07-20 brainstorm)

- **Extend the existing `tickets.issue-work-status` contract** — no new endpoint.
- Fallback extraction: **head branch first, then PR title**.
- `model_name` on the fallback path: **REVA DB lookup first, then default**
  (`helpdesk.ticket`).
- Default instance: **`is_default` boolean column** on `odoo_instances` (migration),
  at most one row set.
- Triggers: **mirror the existing leg** — `pr_active` → `in_progress`, every
  `review_done` → `in_review`, idempotent repeats, no dedup state.
  **Addendum 2026-07-21** (cross-check with ast's reviewed-badge-timesheet spec):
  pushes alone never fire `pr_active` (webhook enqueues only on
  opened/reopened/ready_for_review), which would have left ast's reviewed badge
  set — and its per-cycle review timesheet unbooked — across re-review rounds
  within one PR. New `review_started` trigger, enqueued by `run_review` once a
  paid review is committed to (claimed, under budget): `in_progress` on the
  **work-status leg only** (both sub-legs, per-issue and ticket-level); the
  board leg keeps its two-trigger cadence. One clear→set cycle per actual
  review. No contract change — same payloads, same values.
- Payload: **ticket-level status + PR reference** so Odoo can both flip its REVA
  work-status indicator and log "REVA reviewed PR #42 (link)" in chatter.
- Implementation shape: **extend `board_status_runner` in place** (approach 1);
  extraction/resolution helpers live in `reva/ticket_links.py`.

## Part 1 — contract extension

`IssueWorkStatusPayload` (`reva/odoo_contracts.py`) becomes:

```python
class IssueWorkStatusPayload(BaseModel):
    ticket_id: int
    model_name: str
    issues: list[IssueWorkStatusItem] = []                          # existing per-issue leg
    work_status: Literal["in_progress", "in_review"] | None = None  # NEW: ticket-level
    pr: PrRefPayload | None = None                                  # NEW: {number, title, url, repo}
    # model_validator: at least one of (issues non-empty, work_status set)
```

Same endpoint `POST /tickets/issue-work-status`, same `bearer:instance-outbound-key`
auth. The two legs never mix in one call:

- **Per-issue leg (unchanged):** sends `issues=[...]`, omits the new fields. Every
  payload REVA sends today stays byte-identical — purely additive change.
- **Ticket-level leg (new):** sends `issues=[]` + `work_status` + `pr`.

`OdooCallbackClient.issue_work_status()` gains the two optional kwargs. The
`CONTRACTS` entry gains an `extra_samples` showing the ticket-level shape. Then
`python -m reva.odoo_contracts generate`, commit regenerated `contracts/`, sync
ast-odoo, bump the pin (`test_contracts_drift` enforces all of this).

**ast-odoo side (owed, companion change):** the controller branches on payload shape —
`issues` non-empty → per-issue upsert as today; `work_status` set → set the ticket's
REVA work-status indicator and post a chatter line from `pr`
("REVA reviewed PR #42: <title> (<url>)" for `in_review`; "PR #42 opened" wording for
`in_progress`). Until ast-odoo ships this, ticket-level payloads should be accepted
and ignored gracefully (unknown-field tolerance) or 4xx-rejected — REVA treats a 4xx
as `PermanentError` → ops event, so a premature REVA deploy is visible, not silent.

## Part 2 — fallback resolution in `board_status_runner`

**Hook point:** `run_board_status_update()`. The fallback runs when the per-issue leg
has no REVA ticket to talk to — `resolve_pr_tickets(...)` yields nothing. That covers
both "no closing refs at all" (today's `no_refs` early-return) and "refs exist but
none map to a REVA-created issue". When `resolve_pr_tickets` DOES yield tickets, only
the per-issue leg runs — no ticket-level signal (the per-issue hints already carry
the state).

Existing guards apply unchanged: the merged/closed-PR guard, and the
`RepoConfig.work_status` kill switch (same leg semantically → same flag, **no new
config key**). The board leg is untouched — with no linked issues there are no board
items anyway.

**Extraction ladder** (new pure helpers in `reva/ticket_links.py`):

1. Head branch vs `^(bug|feat|cr|conf|dev|mig|sup|doc)/(\d{1,9})$`, case-insensitive —
   the convention `ticket_issue_runner` writes into issue bodies (`cr/2010`).
2. PR title, two patterns in order: `\[(BUG|FEAT|CR|CONF|DEV|MIG|SUP|DOC)\]\s*(\d{1,9})(?!\.\d)\b`
   (the issue-title format `[CR] 2010 - …`), then a `\b(bug|feat|cr|conf|dev|mig|sup|doc)/(\d{1,9})(?!\.\d)\b`
   token anywhere in the title. The `(?!\.\d)` lookahead keeps a version number like
   `17.0` from being read as ticket `17`.
3. No match → `no_ticket_ref`: normal lifecycle, debug log, **no ops event** (mirror
   of `no_refs` today). 0 and >9-digit ids are rejected (never real ticket ids; the
   9-digit bound also keeps an extracted id from overflowing the DB integer bind).

The type prefix is a work-item type, not an Odoo model — it is only used to anchor
the regex, never mapped to `model_name`.

**Instance + model resolution** (new `resolve_ticket_by_id(db, repo, ticket_id)` in
`reva/ticket_links.py`, returning `(odoo_instance_id, model_name)` or `None` — not a
`TicketRef`, which carries a `run_id` these paths don't have):

1. `ticket_issue_runs` by `(repo_full_name, ticket_id)`, newest first → authoritative
   (`odoo_instance_id` + `model_name`).
2. `ticket_analyses` by `ticket_id` where `github_url` matches the repo, newest first.
3. `ticket_analyses` by `ticket_id` alone, newest first (cross-repo ambiguity is
   theoretical with one instance; newest wins).
4. Nothing in DB → the `is_default` instance + `helpdesk.ticket` (module constant
   `_FALLBACK_MODEL_NAME`; make configurable only if a real need appears).

**Send:** `build_odoo_client(ctx, instance_id)` →
`issue_work_status(ticket_id, model_name, issues=[], work_status=<by trigger>,
pr={number, title, url, repo})`. `pr_active` → `in_progress`, `review_done` →
`in_review`, exactly like the per-issue leg. Idempotent last-signal-wins on the Odoo
side; a PR reopen or second review re-fires — same accepted flapping as the existing
leg.

## Part 3 — `is_default` migration (041)

```sql
ALTER TABLE odoo_instances ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE;
CREATE UNIQUE INDEX IF NOT EXISTS uq_odoo_instances_default
    ON odoo_instances (is_default) WHERE is_default;
```

Numbered file in `db/migrations/`, idempotent, `BIGSERIAL` convention untouched;
matching field on the `OdooInstance` ORM model (tests build from models). The partial
unique index guarantees at most one default.

**Setting the flag is a manual deploy step** (the migration cannot know which row):
one documented SQL line (`UPDATE odoo_instances SET is_default = TRUE WHERE name =
'<cloudunify-prod-name>';`) in the deploy notes, run by Joseph. If step 4 of the
resolution ladder is reached and **no default row exists**: skip the callback + ops
event `no_default_instance` (warning) — misconfiguration must be visible.

## Error handling

Identical discipline to the existing leg:

- `TransientError` → re-raise; RQ retries the whole job (safe: idempotent upsert).
- `PermanentError` (4xx) → warn-log + `record_ops_event(component="odoo_callback",
  severity="warning", event="ticket_signal_rejected", {ticket_id, repo, pr})`.
- Extraction miss / closed PR / `work_status: false` → debug log only, no ops event.
- No `is_default` row when needed → skip + ops event `no_default_instance`.

## TUI

No new surface. Everything this feature produces beyond the callback itself is
delivery outcomes and degradations, and those land in ops events — already surfaced
via `GET /api/v1/ops-events` and the TUI Failures tab.

## Testing

SQLite + mocked GitHub/httpx as usual:

- **Extraction:** branch pattern hits/misses (each type prefix, non-matching
  branches), both title patterns, precedence (branch beats title, `[CR]` form beats
  slash token), case-insensitivity.
- **Resolution ladder:** each of the four steps; multiple candidate rows → newest
  wins; no default instance → skip + `no_default_instance` ops event.
- **Runner:** fallback fires only when `resolve_pr_tickets` is empty; per-issue leg
  behavior unchanged when it isn't (and no ticket-level signal then); both triggers
  map to the right `work_status`; closed-PR guard; kill switch off; `PermanentError`
  → ops event; payload carries `issues=[]` + `pr` ref.
- **Contract:** validator rejects `issues=[]` with no `work_status`; existing sample
  and the new extra sample both valid; drift test forces the regenerated `contracts/`
  to be committed.
- **Honest gap:** the partial unique index is raw SQL — exercised only on real
  Postgres (`make test-integration` or first staging boot), not by SQLite suites.
- **Suites:** worker + api + scheduler (shared `reva/` change) + ruff.

## Out of scope

- Any change to the per-issue leg, the board leg, change notes, or ready signals.
- Auto-setting `is_default` in the migration.
- A richer review-results payload (verdict, findings) — explicitly deferred; this is
  a work-status-style signal only.
- The ast-odoo controller implementation (companion change in that repo).
