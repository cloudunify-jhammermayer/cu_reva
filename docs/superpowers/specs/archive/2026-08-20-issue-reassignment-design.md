# Issue Reassignment — Correcting Which Odoo Record Owns a GitHub Issue

**Status: ✅ IMPLEMENTED (2026-08-20). Not deployed.**

REVA creates GitHub issues against the Odoo record named in the create-issues
request, and from then on treats that record as the issue's owner: every
`/issue-state`, `/issue-work-status`, `/change-summary` and `/ready` callback is
addressed at it. When the issue is on the wrong record, Odoo can move its own
`reva.github.issue` row but REVA cannot be told, so the **next callback
re-creates the issue on the record it was moved off** — Odoo's handler replaces
the record's whole issue set from REVA's union (`_apply_reva_issues`:
`self.reva_issue_ids.unlink()` then create-from-payload).

The Odoo side has shipped a **Move to…** wizard
(`wizard/reva_issue_move_wizard.py`) that calls this endpoint as part of the
move and degrades with a warning note until it exists. This spec is REVA's half.

Source: `Cloudunify/custom_addons/cu_reva_ticket_analysis/docs/reva-side-requests.md`,
request 1 (module 19.0.39.0.0, 2026-08-20). Requests 2 and 3 of that document
shipped separately in commit `2a6d6bc`.

## One correction to the handoff's premise

The handoff attributes the wrong-record case to REVA "parsing the branch name
or PR title (e.g. `cr/1234`)". That is not how an issue gets placed. Branch and
title parsing (`reva/ticket_links.py::extract_ticket_id`) feeds only the
**ticket-level PR work-status signal** in `worker/worker/board_status_runner.py`,
and Odoo's `/issue-work-status` handler upserts existing issue rows only — it
never creates one. Issue placement comes from the create-issues request, which
Odoo itself addressed at a record; adoption and reconciliation both stay scoped
to the same record+repo (`ticket_issue_runner.py`, "adopt the latest prior run's
plan for the same record+repo").

This does not change what to build — the wrong-record case is real either way
(a create-issues run fired from the wrong record) and the remedy is the same.
It does mean **this endpoint will not fix a mistyped branch**: a PR on `cr/1234`
pointing at the wrong ticket still sends its ticket-level signal to the wrong
record, on a path where there is no issue to reassign. That is separate work if
it ever matters.

## Decisions (locked with Joseph 2026-08-20)

- **An override table, not a rewrite of the run rows.** The alternative — move
  the issue item out of every source run and into the target's, synthesizing a
  `ticket_issue_runs` row when the target has none — needs no read-site changes
  at all and is less code. It was rejected because that synthesized row is a run
  that never ran, in a table that also carries model/token/cost columns and
  feeds cost analytics, the TUI run list, and the ticket journey's "created N
  issues" line (which would shift retroactively for the source). The override
  keeps the correction in a small table that says exactly what happened, and
  deleting the row undoes it.

  The cost is accepted knowingly: **every future query over
  `ticket_issue_runs.issues` that resolves an owner must honor the override**,
  and forgetting re-creates precisely the bug this fixes. Mitigation is that all
  five current sites funnel through one helper in `reva/db/writers.py`, and the
  table is named so that a `grep` for it from any new issue-ownership query
  finds them.

- **The endpoint never returns 404.** Odoo's wizard treats `404`/`501` as
  "REVA has not shipped this yet" and commits the move with a warning note on
  both records. If REVA shipped the route and then 404'd an unknown issue — the
  way the sibling `/update-issue-estimate` does — Odoo would commit the move and
  post a warning that is simply false. Unknown issue is therefore `200`;
  malformed body is `422`; and `404` is reserved for a REVA that genuinely lacks
  the route.

- **An unknown issue still stores the override.** The table is a mapping
  override; it does not require the issue to exist yet. Storing it means a
  reassignment that lands before the issue does still redirects correctly. It
  also means a typo'd number is persisted silently, so an unknown-issue
  reassignment records a `warning` ops event.

- **Moving an issue back deletes the override rather than writing an identity
  row.** If the requested target already is the natural owner (the newest run
  carrying the number), any existing override for that key is deleted. Without
  this, records accumulate identity overrides that mean nothing and have to be
  read past forever.

- **No proactive callbacks after a move.** REVA does not push a refreshed issue
  list to either record. Odoo has already written the move itself, the override
  redirects every subsequent callback, and a proactive push would only add a
  409 failure mode against Odoo's `ensure_pending` guards.

- **Cross-model moves are allowed.** A task's issue may move to a ticket and
  back. `model_name` is a plain text column in REVA and nothing constrains the
  pair; Odoo's own model permits it (two nullable host FKs, exactly one set),
  and a wrong-record case does not respect model boundaries in the first place.

- **Ops event, not a TUI column.** A reassignment is a rare manual correction.
  It records an ops event, which `GET /api/v1/ops-events` and the TUI Failures
  tab already surface. A dedicated column or tab would cost more than it shows.

## Shape

### Data model — migration `047_ticket_issue_reassignments.sql`

```sql
CREATE TABLE IF NOT EXISTS ticket_issue_reassignments (
    id               BIGSERIAL PRIMARY KEY,
    odoo_instance_id BIGINT NOT NULL REFERENCES odoo_instances(id),
    repo_full_name   TEXT   NOT NULL,   -- lowercased "owner/repo"
    number           INTEGER NOT NULL,  -- GitHub issue number
    ticket_id        BIGINT NOT NULL,   -- the record that owns it now
    model_name       TEXT   NOT NULL,   -- project.task | helpdesk.ticket
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ticket_issue_reassignments
    ON ticket_issue_reassignments (odoo_instance_id, repo_full_name, number);

CREATE INDEX IF NOT EXISTS idx_ticket_issue_reassignments_record
    ON ticket_issue_reassignments (odoo_instance_id, ticket_id, model_name);
```

One row per moved issue, upserted on the unique key. The second index serves the
"what moved **onto** this record" direction, which is a per-record lookup rather
than a per-issue one.

`odoo_instance_id` is `NOT NULL` even though `ticket_issue_runs.odoo_instance_id`
is nullable for legacy rows: the endpoint is instance-key gated, so every row
this table can ever receive has one.

Follow the numbered-file conventions in `db/migrations/README.md` — idempotent,
`BIGSERIAL PRIMARY KEY` (not `GENERATED … IDENTITY`) to match the existing files
— and add the matching `TicketIssueReassignment` model in `reva/db/models.py`.
Tests build tables from the ORM models, so a missing model makes the table
invisible to every test.

### Endpoint — `POST /api/v1/reassign-issue`

Lives in `api/app/routes/v1/ticket_issues.py` on the instance-gated
`create_router`, beside `/update-issue-estimate`. Synchronous DB write, no RQ
job, no paid call, so no budget check and no `202`.

```
POST /api/v1/reassign-issue
Authorization: Bearer <instance key>

{
  "number": 42,
  "repo": "https://github.com/org/repo",
  "from": {"ticket_id": 1234, "model_name": "project.task"},
  "to":   {"ticket_id": 5678, "model_name": "helpdesk.ticket"}
}
```

`from` is a Python keyword, so the request model needs
`from_record: RecordRef = Field(alias="from")` with `populate_by_name` off —
the wire name is what Odoo sends.

`repo` arrives as a URL and is normalized through
`reva.github_urls.parse_github_repo_url` to the lowercased `owner/repo` the
column stores. A URL that does not parse is `422`, matching how
`/support-request` and `/ticket-analysis` treat `github_url`.

Responses:

| Status | When |
|---|---|
| `200 {"status": "reassigned"}` | override written |
| `200 {"status": "cleared"}` | target is the natural owner; any override deleted |
| `200 {"status": "unknown_issue"}` | no run carries the number; override still written, ops event recorded |
| `422` | malformed body or unparseable `repo` |

`from` is **advisory**: it is logged and included in the ops event, but a
mismatch is not an error. The handoff asks for exactly this — a call whose
`from` no longer matches must still succeed when `to` is already correct,
because the move has already happened.

### Resolution

One helper pair in `reva/db/writers.py`, used everywhere:

- `issue_owner_overrides(db, odoo_instance_id, repo_full_name, numbers)` →
  `{number: (ticket_id, model_name)}` — the per-issue direction.
- `issues_moved_onto(db, odoo_instance_id, ticket_id, model_name)` →
  `[(repo_full_name, number)]` — the per-record direction.

Five sites consume them:

| Site | File | Change |
|---|---|---|
| `update_ticket_issue_state` | `writers.py` | The per-issue `state`/`complete_date` writes into every run carrying the number are unchanged — issue state is a fact about the issue. Only the returned **affected records** list changes: when an override exists it becomes the single override target. The source is deliberately not notified; its union no longer contains the issue, so nothing about it changed. |
| `get_ticket_issue_union` | `writers.py` | Two directions. Drop numbers overridden **away** from this record. Add numbers overridden **onto** it, reading each item from the newest run carrying `(repo_full_name, number)` — found via the override row's repo, since the union itself is keyed on `(instance, ticket_id, model_name)` with no repo of its own. |
| `resolve_pr_tickets` | `ticket_links.py` | A `TicketRef` for an overridden issue targets the override record, keeping the `run_id` of the run that holds the plan (that run is still where the issue's title and body live). |
| `update_ticket_issue_estimate` | `writers.py` | Its run filter is `(ticket_id, model_name)`; for an issue that moved *onto* the record, the run carrying it belongs to the source, so the filter widens to reach that run via the override. |
| `list_ready_tickets` | `writers.py` | Candidates are built from run rows, so a record whose only issues arrived by reassignment would never be considered ready. Override targets join the candidate set. |

`get_board_items_for_issues` needs no change — Projects board placement is
per-issue and never involves a ticket. The `/update-issue-estimate` 404 gate
needs no change either: it reads `get_ticket_issue_union`, so it follows once
the union honors overrides.

### Ops visibility

Per the "degradations are visible" invariant, `writers.record_ops_event`
records:

- `ticket_issues` / `info` / `issue_reassigned` — every accepted move, with
  `number`, `repo`, `from`, `to`.
- `ticket_issues` / `warning` / `reassign_unknown_issue` — the target number
  matches no run for that repo. Visible because a typo'd number is otherwise
  stored silently.

### Contracts

`reassign-issue` is a new **inbound** contract. It needs, in one change:

1. A `Contract(...)` entry in `reva/odoo_contracts.py::CONTRACTS`
   (`direction="odoo->reva"`, `auth="bearer:instance-inbound-key"` — the
   string the other inbound contracts use).
2. An entry in `_inbound_models()` in the same file.
3. An entry in `api/tests/test_contracts_inbound.py::_MODELS` —
   `test_all_inbound_request_contracts_covered` asserts exact set equality, so
   the api suite goes red the moment the route lands without it.
4. `python -m reva.odoo_contracts generate`, then
   `scripts/sync_contracts.sh <odoo-repo>` into `Cloudunify/reva_contracts/`
   and a bump of the `contracts_version` pin in
   `cu_reva_connector/tests/test_contracts.py`.

## Testing

Unit tests only — no Docker, no live Odoo. SQLite in-memory builds the new table
from the ORM model, so the migration's raw SQL is exercised only by
`make test-integration` or the first staging boot; the plan must say so rather
than claim the migration is covered.

The behavioural cases that matter, one test each:

- A reassigned issue's `/issue-state` callback addresses the **target**, and the
  source receives none.
- The source's union no longer contains the issue; the target's does, with the
  item's title/state/estimate intact.
- A target record with **no runs at all** still resolves — the case that breaks
  every naive implementation.
- `resolve_pr_tickets` returns the target for a PR closing the moved issue.
- `/update-issue-estimate` addressed at the target no longer 404s, and writes
  through to the source's run row.
- A record whose only issues arrived by reassignment can reach `ready`.
- Idempotency: the same call twice is a `200` no-op; a call whose `from` is
  stale but whose `to` is right is a `200`.
- Moving back to the natural owner deletes the override.
- An unknown issue number is `200` and records the warning ops event.
- Instance scoping: an override written by one instance does not affect
  another's resolution.

## Accepted imperfections

- **A change note for a moved-on issue carries the source ticket's name.**
  `worker/worker/change_note_runner.py` reads the ticket name off
  `writers.get_ticket_issue_run(ctx.db, ref.run_id)`, and that run still belongs
  to the source record. The note's prompt context is therefore slightly wrong
  for the moved issue. Left as-is: it is prompt colour, not a callback address,
  and fixing it means threading the override through the note builder for a
  rare case.
- **This does not fix a wrong `cr/1234` branch guess** — see the premise
  correction above.
- **`from` is not enforced.** A caller could move an issue it has stale
  information about. The handoff explicitly asks for this, and the alternative
  (409 on mismatch) makes the wizard fail on exactly the retry it is meant to
  survive.
- **A change note generated against the source record before a move can
  become permanently undeliverable.** `maybe_deliver_change_notes`
  (`worker/worker/change_note_delivery.py:45`) only sends once the RECORD's
  own union is non-empty and every issue in it is closed. If a move empties
  the source's union — all its issues get reassigned elsewhere — any note
  still undelivered for the source has no way left to become ready: the
  source has no issues left to close. Left as-is: fixing it means either
  re-targeting undelivered notes onto the record their issues moved to, or
  switching delivery to a convergence signal that survives a move, and this
  reassignment endpoint is not the place to redesign that.

## Prerequisites

None in REVA. On the Odoo side the wizard already ships and already tolerates
the endpoint's absence, so the two sides can deploy in either order.

## Deferred

- Reassigning the **epic** (`ticket_issue_runs.parent_issue`). It is excluded
  from every Odoo payload by design and has no Odoo-side row to move.
- Moving an issue **across Odoo instances**. The unique key is
  instance-scoped and the endpoint is instance-gated, so an override can only
  ever address records in the caller's own instance.
- A TUI surface beyond the ops event.
