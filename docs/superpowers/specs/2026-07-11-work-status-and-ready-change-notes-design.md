# Issue work status to Odoo + change notes on ready — Design

- **Date:** 2026-07-11
- **Status:** approved (design), not yet planned
- **Repos:** cu_reva (this spec) + ast-odoo consumer (companion spec
  `ast-odoo/docs/superpowers/specs/2026-07-11-reva-work-status-ready-notes-design.md`)
- **Context:** requested by Joseph 2026-07-11. Board status sync (eed41f9) moves GitHub
  Projects cards on PR activity, but nothing carries that state to Odoo; change notes
  (`worker/worker/change_note_runner.py`) post to the ticket chatter **per merged PR,
  immediately** — noisy while work is in flight.

## Problem

1. Between "issues created" and "issue closed", the Odoo ticket shows no movement.
   Consultants can't see that a developer started (PR open) or that a PR passed
   review — that visibility exists only on the GitHub board, and only when the
   request carried a board URL.
2. One chatter note lands per merged PR at merge time. Joseph wants **exactly one**
   consolidated note, delivered when the ticket flips ready (all issues closed).

## Decisions (locked)

- Per-issue **work status** (`in_progress`, `in_review`) is sent to Odoo — for every
  REVA-created issue, board or no board.
- Change-note **generation stays at merge time** (freshest diff context, spend spread
  out); **delivery moves to the ready transition**, batched into one callback.
- Ready flag delivery is never delayed by note generation (see the race below).

## Part 1 — work status callback

**New contract** `tickets.issue-work-status` (`reva->odoo`, POST
`/tickets/issue-work-status`):

```json
{"ticket_id": 123, "model_name": "helpdesk.ticket",
 "issues": [{"number": 42, "work_status": "in_progress"}]}
```

`work_status` ∈ `in_progress` | `in_review`. Only the issues linked by the triggering
PR are sent (not the whole union — Odoo upserts by number, existing records only).

**Source:** the existing board-status job (`worker/worker/board_status_runner.py`)
already fires on exactly the right triggers — `pr_active` (PR opened/reopened/
ready_for_review, from the api webhook) and `review_done` (from `runner.py` after a
completed review). It gains an **Odoo leg** that runs independently of the board leg:

- Resolve linked tickets the way `change_note_runner` does (`resolve_pr_tickets` on
  closing refs ∪ the job's GraphQL closing-issues union) — NOT via
  `get_board_items_for_issues`, which requires a persisted `project_item_id` and
  would silently exclude board-less tickets.
- Map trigger → status: `pr_active` → `in_progress`, `review_done` → `in_review`.
- Existing guards apply to both legs: the merged/closed-PR guard (extended to
  `pr_active` per the 2026-07-10 review's open finding) prevents statuses for dead
  PRs.
- Per-instance client via `build_odoo_client(ctx, ref.odoo_instance_id)`; a
  `PermanentError` from Odoo logs + ops-events (`work_status_rejected`, component
  `odoo_callback`) and continues; `TransientError` re-raises for RQ retry
  (idempotent — re-sending the same status is a no-op upsert in Odoo).

**Kill switch:** new `RepoConfig.work_status: bool = True`, checked next to
`board_status_sync` in the job (`_sync_enabled` pattern incl. its ops event).
The two switches are independent: board moves without Odoo callbacks and vice versa.

**Lifecycle notes (accepted semantics):** an issue's `work_status` is a "last signal
wins" hint, not a state machine. Closing the issue supersedes it in the Odoo UI
(consumer hides the badge on closed issues); a PR reopen re-fires `in_progress`; a
second PR on the same issue may bounce it — same accepted flapping as the board.

## Part 2 — change notes delivered on ready

**Generation (unchanged):** merged-PR webhook → `run_change_note` builds and persists
`note_html` per (repo, PR, ticket) in `change_notes`. The job **no longer calls**
`odoo.change_note(...)` at merge time.

**New column:** `change_notes.delivered_at timestamptz NULL` (migration 037,
idempotent `ADD COLUMN IF NOT EXISTS`; ORM model updated — tests build from models).

**Delivery condition (convergent):** deliver the batch when BOTH hold —

1. the ticket is ready (all union issues closed), and
2. every `change_notes` row for the ticket is terminal (`completed` / `failed` /
   `skipped_budget`) — i.e. no note is still generating.

Both events call one shared `maybe_deliver_change_notes(db, ticket_ref)`:

- the ready path in `ticket_issue_runner.py` (~line 486, right after
  `odoo.tickets_ready` succeeds), and
- the tail of `run_change_note` (after a note completes or terminally fails).

Whichever happens **last** triggers delivery. This resolves the inherent race: the
merge that closes the final issue fires the ready signal from the issues webhook
while that same PR's note is still being generated in a parallel job — the ready
flag flips immediately, and the consolidated note follows as soon as the last note
finishes. Ready is never held hostage to a Claude call.

**New contract** `tickets.change-summary` (`reva->odoo`, POST
`/tickets/change-summary`):

```json
{"ticket_id": 123, "model_name": "helpdesk.ticket",
 "notes": [{"pr": {"number": 7, "title": "…", "url": "…", "repo": "acme/widgets"},
            "note_html": "<p>…</p>"}]}
```

Only rows with `delivered_at IS NULL` and `note_html` set are included (failed /
budget-skipped notes are excluded — their PRs still appear in Odoo's PR list via the
traceability wave, just without prose). On Odoo 200, stamp `delivered_at` on the
sent rows in one update. `PermanentError` → ops event `change_summary_rejected`,
rows stay undelivered (retried on the next ready/note event); `TransientError` →
re-raise for RQ retry.

**Post-ready merges** (follow-up PR after the ticket was already ready): the ready
condition already holds when the note completes, so `maybe_deliver_change_notes`
fires from the note job — a batch of one. One late note, still consolidated per PR.

**Reopen → re-ready:** only undelivered rows ship, so a second ready transition
sends a second summary covering only the new PRs. Correct by construction.

**Retired path:** `odoo_client.change_note()` / contract `tickets.change-note`
stays in `contracts/` for one release (the Odoo handler keeps accepting it) but
REVA stops calling it; remove both after the consumer ships. Kill switch stays
`RepoConfig.change_notes` (gates generation AND delivery).

## Contracts / cross-repo

- Add `tickets.issue-work-status` + `tickets.change-summary` payload models +
  registry entries in `reva/odoo_contracts.py`; regenerate `contracts/`;
  `scripts/sync_contracts.sh ../ast-odoo`; bump the addon pin. (Same motion as the
  estimates rollout.)
- Deploy order: Odoo consumer first is preferable (REVA callbacks to an addon
  without the routes → 404 → `PermanentError` → ops event, fail-soft but lossy for
  work status; change-summary rows would stay undelivered and re-send after the
  addon ships — self-healing). REVA-first is therefore safe for notes, degraded
  for work status.

## Explicitly out of scope

- No new terminal states in Odoo's issue-creation state machine; work status is
  display-only.
- No per-issue PR↔issue mapping in the summary (notes stay per-PR).
- No stage automation in Odoo from work status or ready (standing decision).
- Board leg behavior unchanged (except sharing the extended closed-PR guard).

## Testing

- Worker: work-status leg per trigger (mapping, kill switch + ops event, board-less
  ticket included, closed-PR guard, permanent → ops event + continue);
  `maybe_deliver_change_notes` matrix — ready-first / note-first orderings, pending
  note blocks delivery, failed note doesn't, post-ready single-note batch, reopen →
  second batch of only-new rows, `delivered_at` stamped once (idempotent on retry).
- Contracts: wire-shape tests for both payloads; drift test covers regeneration.
- Migration 037 is Postgres-real only via `make test-integration`/staging (as usual).

## Expected behavior after rollout

- Odoo issue lists show live badges: nothing → **In progress** (first PR) →
  **In review** (REVA review done) → closed ✓.
- Ticket chatter: silent during work; ONE "changes merged" summary note at the
  ready moment (or seconds after, when the last note finishes generating); the
  ready flag itself is never delayed.
- Cost unchanged: same note generation per merged PR (~$0.01–0.05 each), zero cost
  for work-status callbacks.
