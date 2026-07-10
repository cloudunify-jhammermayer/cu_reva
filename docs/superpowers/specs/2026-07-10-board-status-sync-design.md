# Board Status follows the PR lifecycle — design

**Date:** 2026-07-10
**Component:** `cu_reva` — GitHub Projects board automation. Touches:
`api/app/routes/webhooks.py` (`_handle_pull_request` enqueue), new
`worker/worker/board_status_tasks.py` job, `worker/worker/runner.py`
(post-review enqueue), `reva/types.py` (`RepoConfig.board_status_sync`).
As built: `worker/worker/board_status_runner.py` has its own read-only
Status resolver, not a module extracted from
`worker/worker/ticket_issue_runner.py` (see Design/Job flow). No DB
changes, no Claude calls.
**Status:** Approved (Joseph, 2026-07-10) — implemented; see plans/2026-07-10-board-status-sync.md.

## Problem

REVA places issues on a GitHub Projects board with Due date, Estimate, and
Status=Todo. GitHub's built-in project workflows handle *added → Todo* and
*closed → Done* (merged PRs auto-close linked issues, so *merged → Done* is
native too). But **no native workflow moves an issue's Status when a linked PR
changes state** — the built-in "review approved"/"PR merged" workflows act only
on PR items on the board, not on issues linked to those PRs. Result: cards sit
in Todo while work is in flight, and the board is only truthful if developers
drag cards manually.

## Context (verified 2026-07-10)

- Per-item projection state (`node_id`, `project_item_id`) is persisted inside
  the `ticket_issue_runs.issues`/`parent_issue` JSON (migration 034);
  `github_project_url` is a run column.
- `GitHubClient` already has `get_project` (fields incl. single-select options)
  and `set_project_item_option`; the runner's board-context helper resolves the
  Status field and option IDs by name (`_STATUS_TODO` lookup).
- `_handle_pull_request` in `api/app/routes/webhooks.py` already reacts to PR
  events (merge → change-note enqueue) — the trigger point exists.
- `runner.run_review` wraps posting a completed review — the second trigger
  point.
- Locked decision from the plan-date spec (2026-07-09): GitHub→Odoo status
  sync is out of scope (company-wide stages undefined); this feature is
  GitHub-side only and does not change that.

## Design

### Scope rule

REVA owns only the transitions native workflows cannot express; it **never
sets Todo or Done** (native domain), never creates Status options, and only
touches issues REVA itself created (they are the only ones with a persisted
`project_item_id`).

### Triggers → one shared job

New RQ job `worker.board_status_tasks.run_board_status_update` with params
`{repo_full_name, pr_number, trigger}`:

- `trigger="pr_active"`: enqueued by `_handle_pull_request` on actions
  `opened`, `reopened`, `ready_for_review`.
- `trigger="review_done"`: enqueued by `runner.run_review` after a completed
  review posts (any conclusion — a reviewed PR is "in review" regardless of
  findings; delta re-reviews included, they are idempotent).

Enqueue from both places (uniform path; GraphQL failures/retries stay isolated
from webhook latency and from the review job's success).

### Job flow

1. Fetch the PR body; parse closing refs with the reviewer's `_ISSUE_REF_RE`
   idiom. When the issue-conformance feature's
   `get_closing_issue_numbers` (GraphQL union, spec 2026-07-10) has shipped,
   union it in — same degrade posture; do not block on that feature.
2. Resolve refs → `ticket_issue_runs` rows for this repo (the
   `resolve_pr_tickets` substrate). No rows → done (optional by
   construction: not a REVA-managed issue, not our board).
3. Fetch repo config; `board_status_sync: false` → done (kill switch below).
   As built, this runs after refs are resolved and board items are found —
   not first — so an unrelated PR with no board items never pays for a
   config fetch.
4. For each run with a `github_project_url`: resolve the board context once
   per job (project + Status field + options via `get_project`). As built,
   the job uses its own read-only Status resolver rather than extracting the
   runner's board-context helper — that helper creates/renames project
   fields, which this feature must never do.
5. For each matched issue item that has a `project_item_id` and is **open**:
   set the Status option — `pr_active` → option named `In Progress`,
   `review_done` → option named `In review` (case-insensitive name match,
   existing options only). Missing option → debug log + skip (board simply
   has no such column — configuration, not degradation).

Idempotent by nature: re-setting the same option is a no-op on GitHub's side;
re-deliveries and RQ retries are safe.

### Config

`RepoConfig.board_status_sync: bool = True` (`.claude-review.yml`), house
kill-switch pattern. Checked in the job, keeping the webhook handler thin
(mirror of `_change_notes_enabled` semantics but without blocking enqueue).

### Failure posture (plan-date precedent: the board is a bonus)

- GraphQL/API failure resolving or setting → log + `record_ops_event`
  (`component="board_status"`, e.g. `event="set_option_failed"`); job swallows
  permanent errors, re-raises transient ones (RQ retry, idempotent).
- Never fails a review, a webhook, or an Odoo callback.
- No TUI surface beyond ops events (Failures tab): the resulting state lives
  on the GitHub board itself; REVA stores nothing new.

## Error handling summary

| Case | Behavior |
|---|---|
| PR has no closing refs / refs aren't REVA issues | Job no-ops (by design) |
| Repo config `board_status_sync: false` | Job no-ops |
| Run has no `github_project_url` / item has no `project_item_id` | Skipped per item |
| Board lacks "In Progress"/"In review" option | Debug log, skip (config, not error) |
| Issue already closed | Skipped (Done is native domain) |
| GraphQL transient failure | TransientError → RQ retry (idempotent) |
| GraphQL permanent failure | Swallow + log + ops event |
| Duplicate webhook delivery / re-review | Idempotent set, harmless |

## Testing

- Unit (worker): trigger→option mapping matrix; kill switch; no-refs no-op;
  closed-issue skip; missing-option skip; missing `project_item_id` skip;
  ops event on API failure; transient re-raise vs permanent swallow; board
  context resolved once per job (call-count on a fake).
- Unit (api): `_handle_pull_request` enqueues on opened/reopened/
  ready_for_review, not on other actions; payload shape.
- Unit (runner): completed review enqueues `review_done`; failed/declined
  reviews do not.
- Staging gate: open a PR with a closing ref to a REVA board issue → card
  moves to In Progress; REVA review completes → In review; merge → Done via
  native workflow (verify no REVA interference).

## Out of scope

- Backward moves (PR closed unmerged leaves the card; documented limitation).
- Creating Status options or fields on the board.
- Issues not created by REVA; PRs on the board as items.
- Odoo ticket stage sync (locked decision, plan-date spec).
