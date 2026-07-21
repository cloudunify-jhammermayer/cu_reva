# Ticket issues — parent issue + GitHub sub-issues

**Date:** 2026-06-19
**Component:** `cu_reva` (worker ticket-issue job, GitHub client, DB). No `ast-odoo` / Odoo-contract changes.
**Status:** Design approved, pending implementation plan

## Problem

REVA turns an Odoo ticket into a set of GitHub issues (the `create-issues`
feature). Today it creates **N flat, independent issues** — there is nothing on
GitHub tying them together as "the work for ticket X". We want one **parent
("epic") issue per Odoo ticket** with the planned issues attached as GitHub
**sub-issues**, so the ticket's work is grouped and its progress is visible at a
glance — while the response REVA sends back to Odoo stays exactly the same.

## Context

Current flow (mapped against the code, 2026-06-19):

- Odoo `POST /api/v1/create-issues` → `ticket_issue_runs` row (`status=pending`)
  → enqueue `run_ticket_issues`.
- `worker/worker/ticket_issue_runner.py::_plan_and_create` resolves the issue list
  (resume own plan → adopt prior plan → reconcile via hidden marker → plan with
  Claude), then creates each issue in a loop (`create_issue`), persisting after
  each one for resume safety. Issues carry a hidden ticket-level dedup marker
  `<!-- revaticket<sha1[:16]> -->` and the `reva-ticket` label.
- Completion → `reva/odoo_client.py::issues_created` posts `/issues-created`
  with `issues: [{number, title, url}]`. GitHub close/reopen webhooks →
  `sync_ticket_issue_state` → `/issue-state` with the full snapshot.

GitHub sub-issues REST API (GA): attach with
`POST /repos/{owner}/{repo}/issues/{parent_number}/sub_issues` body
`{"sub_issue_id": <id>}`. **It requires the child's database `id`, not its
`number`.** `create_issue()` currently discards `data["id"]`. Documented limits
(100 sub-issues/parent, 8 nesting levels) are far above our 10-issue cap.

### Locked decisions

1. **Parent excluded from the Odoo payload.** The parent lives only on GitHub;
   `/issues-created` and `/issue-state` still carry only the N child issues, so
   Odoo is byte-for-byte unchanged and needs no code change.
2. **Parent synthesized locally** from the Odoo ticket — no Claude/planner/prompt
   change (keeps prompt-cache hashes stable, adds no paid call).
3. **Skip the parent when the plan has exactly 1 issue** (a parent wrapping one
   child is noise).

### Explicitly out of scope (rejected this session)

- REVA must **not** mark the Odoo ticket done when the parent issue closes.
  Ticket completion is a manual deployment step. No new Odoo callback, no Odoo
  ticket-status mutation.

## Design

### Behavior

When the resolved plan has **≥2** issues: create the parent first, then create
each child, then attach each child to the parent. When the plan has **1** issue:
behave exactly as today (no parent). The Odoo callbacks are unaffected in both
cases.

> **Update 2026-07-03:** the single-issue exception was removed — every plan
> that still has issues to create gets the epic (created or adopted), so
> single-issue requests attach too. Only pre-existing fully-created flat sets
> stay flat (no backfill). Decision 3 and the "1 issue → no parent" test below
> describe the original rollout.

### `reva/github_client.py`

- `create_issue()` → additionally return `id` (capture `data["id"]`).
- new `add_sub_issue(token, owner, repo, parent_number, sub_issue_id)` →
  `POST /repos/{owner}/{repo}/issues/{parent_number}/sub_issues` with
  `{"sub_issue_id": sub_issue_id}`. Treat HTTP 422 "already has this sub-issue"
  as success (idempotent re-attach on resume).
- `find_issues_with_marker()` → additionally return `id` (needed to re-attach
  children to the parent during DB-wiped reconciliation).

### `worker/worker/ticket_issue_runner.py` (`_plan_and_create`)

- new `_format_parent_body(params, marker)` — synthesized locally (decision 2):
  body = Odoo back-link (`params.name` → `params.ticket_url`) + the standard
  `<!-- revaticket<digest> -->` marker + a distinct `<!-- revaticket-parent -->`
  tag. GitHub renders the sub-issue checklist itself, so we do not hand-build a
  child list in the body.
- parent title: `[Task 123] <ticket name>` (reuses the existing `[Label id]`
  prefix convention from `_issue_title`, without the `n/total` order marker).
- Order, all resume-safe (each step persists before the next GitHub call):
  1. ensure parent exists (create if `parent_issue` is null/uncreated), persist it;
  2. create each child that has no `number` (unchanged loop), persist after each;
  3. attach each child whose `attached` flag is not set, then set the flag and
     persist.
- `_issues_payload()` is unchanged → the parent never enters the Odoo payload
  (decision 1).
- single-issue plans skip steps 1 and 3 entirely (decision 3).

### Persistence — `reva/db/`

- New migration `017_ticket_issue_parent.sql`:
  `ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS parent_issue JSONB;`
  (idempotent; `BIGSERIAL`/style conventions already established for this table).
- Matching nullable field on the `TicketIssueRun` ORM model in
  `reva/db/models.py` (tests build from the models, so the field must exist there).
- `parent_issue` shape: `{number, id, url, state}`; `NULL` for legacy and
  single-issue runs.
- Per-child `attached: bool` added to each item in the existing `issues` JSON
  (alongside `number`/`url`/`state`), so the attach step is independently
  resumable.
- Writers: a setter to persist the parent (mirrors `update_ticket_issue_progress`);
  ensure `purge_old_ticket_issue_text` (SECU-8) **preserves** `parent_issue`'s
  `number`/`url`/`state` (it only strips customer text like bodies/criteria).

### Idempotency & reconciliation (the two real hazards)

- **Attach resume:** the stored `attached` flag means a retry attaches only the
  not-yet-attached children; combined with the 422-as-success rule, double
  attaches are impossible. (Chosen over a `GET .../sub_issues` per resume, which
  costs an extra round-trip.)
- **DB-wiped reconciliation:** today `find_issues_with_marker` returns *every*
  marked issue and treats them all as children. The parent carries the same
  marker, so reconciliation **must split the parent out** via the
  `revaticket-parent` tag before building the child set — otherwise the parent
  leaks into the Odoo payload, violating decision 1. After splitting: return the
  children to Odoo as today, and (best-effort) re-attach any detached children to
  the recovered parent.

### Labels & webhooks

The parent gets the existing `reva-ticket` label (so `label:reva-ticket`
filtering still shows it). A parent close/reopen webhook therefore fires
`sync_ticket_issue_state`, which looks the number up among the runs' `issues[]`,
finds no match (the parent is not a child), and no-ops — harmless, and
consistent with "no auto-done".

## Error handling

- Sub-issue attach failures follow the existing `TransientError`/`PermanentError`
  split: a transient attach error rides the RQ retries (the run stays pending and
  resumes from the persisted plan + flags); a permanent one fails the run and
  sends the best-effort failed callback, exactly as today.
- 422 "already a sub-issue" on attach is swallowed as success.
- If the GitHub App token cannot call the sub-issues endpoints (newer than plain
  issue creation), the attach raises like any other GitHub error and surfaces in
  the run — see verification.

## Testing / verification

Unit-tested against the existing harness (SQLite + `httpx` MockTransport +
subprocess mocks — no Docker/network):

1. **≥2 issues** → parent created once, all children created, each attached with
   its `id`; `/issues-created` payload contains only the children (no parent).
2. **1 issue** → no parent created; behavior identical to today.
3. **Resume after partial failure**: parent already created + some children
   created/attached → rerun creates/attaches only the remainder; no duplicate
   parent, no double attach (422 path covered).
4. **Reconciliation (no DB plan, marked issues exist)**: parent is split out via
   the `revaticket-parent` tag; only children are returned to Odoo.
5. `create_issue`/`find_issues_with_marker` return `id`.

Definition of done (per CLAUDE.md): `worker` + `reva` suites green; because
`reva/` is shared, also run `api` and `scheduler` (`make test`); plus `ruff`.
The Postgres-only migration (`017`) is exercised on first staging boot /
`make test-integration`, not the SQLite unit suite — state this honestly.

**TUI:** if the ticket-issue data is surfaced in `tui/`, show the parent issue
link there too (extend the backing `/api/v1` endpoint as needed) and keep
`go build/vet/test ./...` green. If ticket-issues are not yet surfaced in the
TUI, no TUI change is required — note which is the case during implementation.

**Staging check:** confirm the installation token can call `POST .../sub_issues`
on the first real run (the endpoint is newer than issue creation).

## Out of scope

- Any Odoo-side change (auto-done, contract changes, new callbacks).
- Planner/prompt changes (parent is synthesized locally).
- Multi-level nesting; reprioritizing sub-issues.
- Backfilling parents onto already-created (pre-feature) runs.
