# Change summary follow-ups: seven improvements — design

Status: IMPLEMENTED · Date: 2026-09-30 · Repos: cu_reva (this) + Cloudunify `cu_reva_ticket_analysis` (one test).
Follows `archive/2026-09-29-change-summary-modules-design.md`, whose whole-change review and the questions around it
produced this list. Decision (Joseph, 2026-09-30): all seven, in the same change as the feature — one commit per repo.

## Need

The change-summary feature ships branch-linked tickets and per-merge summaries. Seven gaps remain around it: a job
that depends on GitHub when it no longer needs it, summaries nobody can deploy, tickets routed to the wrong Odoo
instance, notes stuck forever, tickets the TUI cannot show, a release name lost on requeue, and an untested header
path.

## Behaviour

### 1. Reuse the stored module lists

`worker/worker/change_note_runner.py`: before the GitHub lookup, the job reads the lists already stored for this PR
(`writers.get_stored_change_note_lists(db, repo, pr_number)`: the `modules` / `submodules` of any `change_notes` row
of the PR that has them; they are per PR, identical across its tickets). Only when none is stored does it call
`_affected_modules`. The lookup stays before any row is written, so a `TransientError` still leaves no pending row
behind. A re-run (RQ retry, budget deferral, re-delivery) makes no changed-files call.

### 2. Header test on `project.task` (Odoo)

One test in `tests/test_callback.py`: a task without REVA issues gets "Changes merged"; the same task with a REVA
issue gets "Changes merged — ready for review/deploy". No production change, no version bump.

### 3. The repo's declared Odoo instance in the branch fallback

`.claude-review.yml` on the repo's default branch may declare `odoo_instance: <name>` (it already maps repos to
instances for the release-log lookup). `resolve_ticket_by_id` gains `instance_id: int | None = None`:

- rung 1 (a `ticket_issue_runs` row for this repo and ticket) is unchanged: a recorded run is ground truth;
- with `instance_id` given, the `ticket_analyses` rungs only consider rows of that instance, and the last rung
  returns `(instance_id, default_model)` instead of the default instance;
- without it, the ladder is today's.

Both callers of the ladder resolve the declared instance through one helper (`worker/worker/repo_instance.py`):
the change-note fallback and the work-status fallback in `board_status_runner`.

| Case | Result |
|---|---|
| repo not registered, no config file, or no `odoo_instance` key | `None`: today's ladder |
| declares `X`, an active instance named `X` exists | its id |
| declares `X`, no active instance named `X` | no ticket; ops event `odoo_callback` / `warning` / `unknown_repo_instance` |
| config cannot be read (unparseable YAML, top level not a mapping, `odoo_instance` not a non-empty string, non-transient GitHub error) | no ticket; ops event `odoo_callback` / `warning` / `repo_instance_config_failed` |
| `TransientError` | propagates, RQ retries |

Never falling back to the default instance when the repo names another one is the point: a drafted summary of a
customer's diff must not land in a different tenant's Odoo. For the same reason the key is read from the raw file, not
through the validated config: an invalid value in an unrelated key must not make the repo look as if it declared no
instance.

### 4. No draft for a branch-linked PR with nothing to deploy

When every ticket of the job was resolved through the branch or title (`TicketRef.run_id is None`) and the lookup
succeeded with no module and no submodule, the job ends with `{"status": "nothing_to_deploy"}` before any row is
written: no Claude call, no chatter note. A failed lookup (`None`) does not count as empty. Issue-linked tickets are
unchanged. Normal lifecycle, so an info log and no ops event.

### 5. Reap stuck pending notes

A `change_notes` row left `pending` by a killed worker blocks every later summary for its ticket.

- `writers.reap_stale_pending_change_notes(db, older_than_seconds) -> list[dict]` marks rows `pending` for longer
  than the threshold as `failed` (`error_message` "Reaped: stuck in 'pending' …"), `FOR UPDATE SKIP LOCKED` like the
  review reaper, and returns them.
- The scheduler calls it every loop. Per reaped row: ops event `change_note` / `warning` / `stale_pending_reaped`.
  Per affected ticket: one RQ job `worker.change_note_tasks.deliver_change_notes` so the notes it was blocking ship.
  An enqueue failure is logged and recorded (`change_note` / `warning` / `reaper_enqueue_failed`); the next merge or
  ready event delivers.
- Threshold: `REVA_BUDGET_WAIT_MAX_SECONDS` (default 172800) + `REVA_BUDGET_RETRY_SECONDS` (default 3600) + 7200. A note legitimately stays pending for the whole
  budget wait, so anything shorter would reap live work. The scheduler reads the same env var the worker reads; both
  compose files pass it to the scheduler.
- `deliver_change_notes({"odoo_instance_id", "ticket_id", "model_name"})` builds the Odoo client and calls
  `maybe_deliver_change_notes`.

### 6. Change notes in the API and the TUI

- `GET /api/v1/change-notes?limit=&offset=` (master key, like the other TUI feeds): change notes newest first,
  `{items, total}`. Item: `id`, `repo_full_name`, `pr_number`, `pr_title`, `pr_url`, `odoo_instance_id`,
  `ticket_id`, `model_name`, `status`, `source`, `modules`, `submodules`, `error_message`, `estimated_cost_usd`,
  `created_at`, `completed_at`, `delivered_at`. No `note_html`.
- TUI Tickets tab: a third feed beside analyses and issue runs. A record that has change notes only gets a row,
  grouped under the note's repo; its ISSUES cell reads `N notes` (the record's note count in the feed). `enter` on a
  row without issues but with notes opens the detail pane with the journey. Rows that have an analysis or a run look
  as today.
- Demo mode carries one notes-only ticket.

### 7. The release survives a requeue

`ticket_issue_runs` gains `release_id BIGINT` and `release_name TEXT` (migration 053), written when the run is
created. The requeue route rebuilds `release` from them, so requeued issues carry the `**Release:**` line again.
Rows from before the migration have NULL and requeue without a release, as today.

## Out of scope

- Base-branch filter or naming the target branch (still open with Joseph).
- Marking deleted modules.
- A TUI view of a single note's text.
- Restricting rung 1 of the ladder by the declared instance.

## Tests

Unit tests per item in `worker/tests`, `api/tests`, `scheduler/tests`, `tui` (`go test ./...`), one Odoo test.
Migration 053 runs on Postgres through `make test-integration`. Not covered: a live GitHub config fetch, a live RQ
`deliver_change_notes` round trip.

## Deploy

No order constraint beyond the feature's own (Odoo first). Migration 053 at boot; api, worker and scheduler images
rebuilt; a new TUI binary for the change-notes rows.
