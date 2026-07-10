# Ticket issues — GitHub Projects, plan date, complete date

**Date:** 2026-07-09
**Component:** `cu_reva` (api create-issues route, worker ticket-issue job, GitHub client, DB, TUI) **+ Odoo contract change** (inbound `create-issues`, outbound `tickets.issue-state`) → contracts regen + ast-odoo re-sync required.
**Status:** Approved — implemented 2026-07-09 (see plans/2026-07-09-github-projects-plan-date.md; staging gate + ast-odoo contract re-sync still open).

## Problem

Odoo will now send, with every `create-issues` request, an optional **GitHub
Project URL** and an optional **plan date**. Every issue REVA creates for that
request (children **and** the parent epic) must be added to that project with
the plan date set as a project date field. When either field is absent, behavior
is exactly as today.

Additionally, Odoo renders per-issue **Planned Date** / **Completed Date**
columns: every issue ref REVA sends back (`issues-created`, `issue-state`,
`tickets-ready` snapshots) carries `plan_date` (echo of the request's plan
date) and `complete_date` (the date the issue was closed; cleared on reopen).

## Context

Current flow (mapped against the code, 2026-07-09):

- Odoo `POST /api/v1/create-issues` (`api/app/schemas/ticket_issues.py::CreateIssuesRequest`,
  "optional additive fields are fine") → `ticket_issue_runs` row → RQ
  `run_ticket_issues`.
- `worker/worker/ticket_issue_runner.py::_plan_and_create`: resolve issue list
  (resume → adopt → reconcile → plan), then resume-safe steps: ensure labels →
  parent epic → children → sub-issue attach (`attached` flag per item, persisted
  after every GitHub call).
- Close/reopen: GitHub `issues` webhook (`api/app/routes/webhooks.py::_handle_issues`)
  enqueues `sync_ticket_issue_state` with `{owner, repo, number, state}` — the
  payload's `issue.closed_at` is currently dropped. The worker posts
  `/tickets/issue-state` (`reva/odoo_client.py::issue_state`, `IssueStatePayload`).
- `reva/github_client.py` already has GraphQL plumbing (`_graphql_data`,
  `get_review_threads`, `resolve_review_thread`).

### GitHub Projects (v2) facts the design leans on

- **GraphQL-only.** Resolve `https://github.com/orgs/{org}/projects/{n}` (or
  `/users/{user}/projects/{n}`) via `organization(login){projectV2(number)}` /
  `user(login){projectV2(number)}` → project node id + fields.
- `addProjectV2ItemById(projectId, contentId)` — **idempotent** (re-adding
  returns the existing item id). Keys on the issue's GraphQL **node id**;
  `create_issue()` currently returns only the numeric database `id`.
- Field values via `updateProjectV2ItemFieldValue`: `{date}`,
  `{singleSelectOptionId}`. Fields created via `createProjectV2Field`
  (`DATE`, `SINGLE_SELECT` — single-select options require name+color+description).
- **A fresh project has NO date field by default** (the built-in fields are
  Title/Assignees/Status/Labels/…; the roadmap layout *prompts* you to create
  "Start date"/"Target date" on first use). So REVA cannot assume one exists —
  it reuses one if present, else creates it (same philosophy as `ensure_label`).
- **App permission:** installation tokens need the org-level **Projects:
  Read & write** permission. Adding it to the App requires each installation to
  re-approve — rollout friction, handled fail-soft below.
- Built-in project workflows (*item closed → Done*, *PR merged → Done*,
  auto-add by filter) are project-side config, no API involvement.

### Locked decisions (session 2026-07-09)

1. **Scope:** project add + plan date + Status/Priority field mapping + docs for
   built-in workflows/roadmap/board setup + TUI visibility. **GitHub→Odoo status
   sync is explicitly out of scope** — company-wide stages are not defined yet.
2. **The parent epic gets the plan date too** (an undated epic looks broken on
   the roadmap) and is added to the project like the children.
3. **Auto-provision fields:** REVA uses a custom `Due date` DATE project field —
   reuse an existing `Due date`, else rename a legacy `Plan date` (or a case
   variant `due date`) field to `Due date` in place (values preserved), else
   create `Due date` when the request carried a date. GitHub's built-in roadmap
   `Start date`/`Target date` are issue-backed and **reject**
   `updateProjectV2ItemFieldValue` (discovered empirically), so REVA never
   targets them.
4. `complete_date` is always derived from GitHub's `closed_at`; `null` on
   reopen. **Per-issue, not top-level** — see the alignment note below.
5. **Fail-soft:** a Projects failure never fails the run or blocks the Odoo
   callback — issues are the product, the board is a bonus. Every degradation
   logs **and** `record_ops_event` (CLAUDE.md degradation invariant).
6. **Wire alignment with the shipped Odoo addon (verified in ast-odoo
   2026-07-09, `cu_reva_ticket_analysis` 19.0.11.2.0, uncommitted on `dev`):**
   the addon already implements the consumer side. Board URL is sent as
   `github_project_url` (renamed on the Odoo side from `github_project` to
   match this spec; the internal setting stays `reva.github_project` and is
   **global**, one board for all requests). Dates are **per-issue fields on
   the issue refs** (`plan_date`, `complete_date`, `YYYY-MM-DD`) — Odoo
   stores them as `issue_plan_date`/`issue_complete_date` on
   `reva.github.issue` and shows them as list columns. Per-issue beats a
   top-level `complete_date`: the callbacks send full union snapshots, so
   every closed issue keeps its date and unions across runs with different
   plan dates stay correct. The addon truncates incoming values to
   `YYYY-MM-DD` defensively, but REVA sends plain dates (UTC date of
   `closed_at`).

## Design

### Contracts (`reva/odoo_contracts.py` + `api/app/schemas/ticket_issues.py`)

Inbound `create-issues` — two optional additive fields (empty string → `None`,
matching the existing `issue_type`/`github_username` validators):

- `github_project_url: str | None` — validated at the route with the new parser
  (invalid non-empty value → 422, same policy as `github_url`).
- `plan_date: date | None` — ISO `YYYY-MM-DD`.

Outbound — **per-issue** (decision 6): `IssueRefPayload` gains
`plan_date: str | None` and `complete_date: str | None` (both `YYYY-MM-DD`).
Every callback that carries issue refs (`issues-created`, `issue-state`,
`tickets-ready`) therefore includes them; `_ISSUE_KEYS` in `reva/odoo_client.py`
is extended to exactly these two additional keys (`node_id`/`project_item_id`
still never leak to Odoo). `plan_date` is stamped on each item at creation time
from the request's plan date (so unions across runs with different plan dates
stay per-issue correct); `complete_date` is stamped by the state sync when an
issue closes (UTC date of GitHub's `closed_at`) and cleared to `null` on
reopen. `IssueStatePayload` itself gains no top-level field.

Both flow into `TicketIssueJobParams` (`run_id`-persisted, so requeues resume
with them). Regenerate `contracts/` (`python -m reva.odoo_contracts generate`)
and re-sync ast-odoo (branch from `dev` → PR to `dev`).

### `reva/github_urls.py`

New `parse_github_project_url(url) -> tuple[str, str, int] | None` returning
`(owner_type "orgs"|"users", owner, number)`. Tolerates whitespace, trailing
slash, and a `/views/{n}` suffix; rejects other hosts/paths. Shared by the api
route (422 at accept time) and the worker.

### `reva/github_client.py`

- `create_issue()` / `find_issues_with_marker()` / `get_issue()` → additionally
  return `node_id` (GraphQL id; already present in the REST responses).
- `get_project(token, owner_type, owner, number) -> {"id", "fields"}` — one
  GraphQL query; `fields` = `[{id, name, dataType, options?}]`
  (`ProjectV2FieldCommon` + `ProjectV2SingleSelectField` fragments, `first: 50`).
- `create_project_field(token, project_id, name, data_type, options=None) -> field dict`
  (`DATE` for Plan date; `SINGLE_SELECT` with name/color/description options
  for Priority).
- `add_issue_to_project(token, project_id, issue_node_id) -> item_id`
  (`addProjectV2ItemById`; idempotent by API contract).
- `set_project_item_date(token, project_id, item_id, field_id, date)` and
  `set_project_item_option(token, project_id, item_id, field_id, option_id)`
  (`updateProjectV2ItemFieldValue`).

All raise through the existing `_graphql_data` Transient/Permanent split; the
**runner** decides fail-soft, not the client.

### Field resolution policy (per project, resolved once per run and cached in-run)

| Purpose | Lookup (case-insensitive) | If missing |
|---|---|---|
| Due date | `Due date`, then a legacy `Plan date`/`due date` (`DATE` field) — a legacy match is renamed to `Due date` in place, values preserved | create `Due date` (DATE) when the request carried a date |
| Status → `Todo` | built-in `Status`, option named `Todo` | option missing → skip + ops event (never mutate existing options) |
| Priority | single-select `Priority`, options matched by name `Low/Medium/High/Urgent` | field missing → create with those four options; field exists but option name unmatched → skip + ops event |

Odoo `priority` mapping (already in every request, Contract 1): `"0"`→Low,
`"1"`→Medium, `"2"`→High, `"3"`→Urgent. The parent epic gets the same priority
(priority is per-request, not per-item).

Existing single-select **options** are never modified (no option rewrites via
`updateProjectV2Field` — replacing options is destructive to the customer's
board); the sole exception is renaming a legacy date field to `Due date` in
place, which preserves its values. Skips are visible: `record_ops_event("github",
"warning", "project_field_unmatched", {...})`, once per run per field.

### `worker/worker/ticket_issue_runner.py` — step 4 of `_plan_and_create`

After the sub-issue attach loop, when `params.github_project_url` is set:

1. Resolve project + fields once (`get_project`); ensure Plan date / Priority
   fields per the policy table.
2. For the parent and each child **without a persisted `project_item_id`**:
   `add_issue_to_project` → set Plan date (when `plan_date` given) → set
   Status=`Todo` → set Priority → persist `project_item_id` on the item
   (same pattern as the `attached` flag).
3. The **persisted `project_item_id` is the "don't touch again" guard**: on
   resume/adoption the fields are not re-set, so a card a developer already
   moved out of `Todo` is never reset. (DB-wipe reconciliation loses the ids →
   a re-add would reset Status; accepted, same narrow window as the existing
   marker-search caveats.)
4. Items that predate the feature carry no `node_id`; when a later request
   arrives with a project URL, fetch it via `get_issue(number)` and project
   them too — **a re-click after configuring the project backfills the board.**
   Consequently the "done, skip GitHub round-trips" short-circuit additionally
   requires: no project URL, or every item (and the parent) has a
   `project_item_id`.
5. The whole step is wrapped fail-soft (decision 5): any error → log +
   `record_ops_event("github", "warning", "project_item_failed", {...})` +
   continue; the run still completes and the `issues-created` callback is
   unchanged (project membership is never sent to Odoo).

`plan_date` is written at projection time only; a later request with a
different `plan_date` dates its *new* issues but does not re-date existing
items (see out of scope).

### Per-issue date pipeline

- **`plan_date` echo:** the runner's creation loop stamps
  `"plan_date": params.plan_date.isoformat()` (or `None`) on each created
  item; adopted items keep their originating run's value, reconciled-from-
  GitHub items have none.
- **`complete_date`:** `_handle_issues` (webhook) passes
  `closed_at = issue.get("closed_at")` into the `sync_ticket_issue_state` job
  params (absent/None on reopen). `update_ticket_issue_state` stamps
  `item["complete_date"] = closed_at[:10]` when closing / `None` when
  reopening, alongside the existing `state` stamp across all runs carrying the
  issue.
- `get_ticket_issue_union` and the callback ref projection (`_ISSUE_KEYS`)
  carry both keys through to Odoo.
- Older queued jobs without the `closed_at` key default to `None` (use `.get`,
  not a required param — no PermanentError for in-flight jobs during deploy).

### Persistence — `reva/db/`

- Migration `034_ticket_issue_project.sql` (idempotent, `ADD COLUMN IF NOT
  EXISTS`): `ticket_issue_runs.github_project_url TEXT`,
  `ticket_issue_runs.plan_date DATE`.
- Matching nullable fields on the `TicketIssueRun` ORM model (tests build from
  models).
- Per-item `node_id` and `project_item_id` live inside the existing `issues`
  JSON and `parent_issue` JSON — no schema change; the retention purge
  (SECU-8) must preserve both (they are not customer text).
- Writers: persist the new request fields at row creation; progress updates
  unchanged (`update_ticket_issue_progress` already round-trips whole items).

### GitHub App permission rollout

Add org-level **Projects: Read & write** to the GitHub App; each installation
must approve. Until approved, every Projects GraphQL call fails → the fail-soft
path records ops events and issues keep flowing. Staging check: first real run
against a test project confirms resolve/add/field-set with an installation
token.

### TUI (CLAUDE.md invariant 5)

- `api/app/schemas/ticket_issues.py::TicketIssueRunSummary` (+ status view):
  add `github_project_url: str | None` and `plan_date: date | None`.
- `tui/internal/api/types.go` + `mock.go`: matching fields; `tui/internal/ui/tickets.go`
  detail view shows the project link and plan date (compact "📋 project · due
  2026-07-15" line, matching existing detail rows).
- Projects degradations surface automatically via the existing ops-events →
  Failures tab path.
- `cd tui && go build ./... && go vet ./... && go test ./...` stay green.

### Documentation (workflows 1, 3, 5 — config, no code)

New `docs/github-projects.md` (linked from the github-issue-creation doc):

- Enable built-in workflows *item closed → Status: Done* and *linked PR
  merged → Done* — with these plus REVA's Status=Todo on add, the board tracks
  reality with zero further API writes.
- Roadmap view: set the date field to **Plan date** → timeline of all
  Odoo-planned work.
- Board/table: group by **Parent issue** → one swimlane per Odoo ticket with
  the epic's sub-issue progress; slice by the `Priority` field or type labels.
- The App-permission re-approval step.

## Error handling

- **Everything Projects-related is fail-soft** (decision 5): Transient *and*
  Permanent GraphQL errors degrade to log + ops event; the run completes and
  Odoo callbacks are untouched. A transient miss is not retried within the run
  — the persisted `project_item_id` gap means the next request for the ticket
  (or a re-click) heals the board.
- Issue creation, sub-issue attach, and callbacks keep their existing
  Transient/Permanent semantics — unchanged.
- `complete_date` adds no new failure modes: it rides the existing
  `issue-state` retry policy.

## Testing / verification

Unit-tested on the existing harness (SQLite, httpx MockTransport, no network):

1. Schema: empty-string → None for both new fields; invalid
   `github_project_url` → 422; valid orgs/users/views URLs parse.
2. Contract drift tests + regenerated `contracts/` committed.
3. Runner: request with project URL + plan_date → parent + children each get
   `addProjectV2ItemById` + date/Status/Priority mutations, `project_item_id`
   persisted per item; resume re-projects only unprojected items and never
   re-sets fields on projected ones.
4. Runner fail-soft: GraphQL error on add → run completes, callback sent,
   ops event recorded.
5. Backfill: adopted fully-created run + new request with project URL →
   `node_id` fetched via `get_issue`, items projected, no new issues created.
6. Field policy: existing `Target date` reused; missing Priority created;
   unmatched `Todo` option skipped + ops event.
7. Webhook: closed payload → job params carry `closed_at`; reopened → None;
   the issue-state snapshot items carry per-issue `complete_date`
   (`closed_at[:10]`, cleared on reopen) and the creation-time `plan_date`
   echo; legacy job params without the key still work; `_ISSUE_KEYS` sends
   exactly the six documented keys.
8. No project URL → byte-identical behavior to today (regression suite).

Definition of done: `make test` (shared `reva/` touched → worker + api +
scheduler), `ruff`, contracts regenerated + ast-odoo synced, TUI
build/vet/test green. Postgres-only migration `034` is exercised by
`make test-integration` / first staging boot, not the SQLite suite — state
honestly. Staging: verify the installation token after the App-permission
approval; confirm one real project gets item + fields.

## Out of scope

- **GitHub→Odoo status sync** via `projects_v2_item` webhooks (stages not
  defined company-wide yet) — revisit later.
- Re-dating already-projected items when a later request carries a different
  `plan_date`; removing items from projects; multiple projects per request.
- Setting/maintaining Status beyond the initial `Todo` (the built-in project
  workflows own Done transitions).
- Mutating existing project fields/options (destructive to customer boards).
- Any Odoo-side implementation (ast-odoo consumes the regenerated contracts).
