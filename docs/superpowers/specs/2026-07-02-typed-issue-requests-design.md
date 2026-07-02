# Typed Issue Requests from Odoo + Unified Title Convention — Design

Date: 2026-07-02
Status: approved (design Q&A with Joseph, 2026-07-02)

## Context

REVA already creates GitHub issues from Odoo tickets: `POST /api/v1/create-issues`
(per-instance Bearer) → `ticket_issue_runs` row (row id = `request_id`) → RQ job
`run_ticket_issues` → Claude planner (`prompts/ticket_issues.md`) → issues posted with
label `reva-ticket` + hidden dedup marker → callback `POST {odoo}/api/reva/issues-created`
→ GitHub `issues` webhook keeps per-issue open/closed state synced via
`POST {odoo}/api/reva/issue-state`. Titles are built in code
(`worker/worker/ticket_issue_runner.py:_issue_title`): `[Task 18423] 2/10 — <title>`.

The Odoo addon `cu_reva_ticket_analysis` (in `../ast-odoo/custom_addons/`, currently
19.0.8.1.0) holds the issue list as `reva.github.issue` records on
`helpdesk.ticket` / `project.task`, driven by an aggregate state machine
`reva_issue_status` (none/pending/created/failed) + `reva_issue_request_id`.

### New capability

A consultant can send an ad-hoc **issue request** from a ticket: a wizard with an
*optional* type code and a free-text request. REVA plans 1..N issues from that text
(splitting like the analysis flow when the text warrants it), posts them, reports back
to Odoo, and the issues join the ticket's existing issue list and epic.

At the same time, **all** REVA-created issues move to a new title convention.

## Decisions (from design Q&A)

1. **Title convention** (all flows): `[TYPE] <ticket_id> - <tldr>` with tldr ≤ 30 chars.
   Multi-issue requests append the sequence: `[FEAT] 18423 - Add delivery report (1/3)`.
   The `(n/total)` suffix is omitted when a request yields exactly one issue.
2. **Type codes**: exactly `BUG FEAT CR CONF DEV MIG SUP DOC`. No FB code.
3. **Type resolution**: if the request carries `issue_type`, every issue created from it
   gets that type. If absent (analysis flow, or wizard sent without a type), the planner
   picks a type per issue from the 8 codes based on content.
4. **Reuse the existing endpoint**: no new route. `POST /api/v1/create-issues` gains one
   *optional* field `issue_type`. The wizard flow is a normal create-issues run whose
   `description` is the wizard text and whose `analysis_html` is `""`.
5. **One epic per ticket**: new issues attach as sub-issues to the ticket's existing
   parent issue when one exists (even a single new issue). A new parent is created only
   when none exists yet **and** the request yields ≥ 2 issues; it then becomes the
   ticket's epic for future requests.
6. **Type as GitHub label**: each issue gets its type code as a label alongside
   `reva-ticket`.
7. **Claude formats the request**: the planner produces the ≤30-char tldr and a clean,
   structured English issue body from the raw wizard text (existing planner rules:
   keep all facts, no invention, English output).

## Integration hazards being fixed (pre-existing)

Two Odoo-callback semantics assume "one run owns the whole list" and would corrupt the
list once a ticket accumulates issues across multiple requests:

- **`/issues-created` replaces the list.** Odoo's `_apply_reva_issues` unlinks every
  `reva.github.issue` and recreates from the payload; on `failed` it unlinks everything
  and keeps nothing.
- **`/issue-state` reconciles against a full snapshot** and deletes records missing from
  it — and REVA currently sends only the newest run's snapshot.

Fix (REVA side, keeps the Odoo replace-handler simple): both callbacks send the
**union of issues across all runs** of the ticket — every issue with a GitHub number
across `ticket_issue_runs` rows matching `(odoo_instance_id, ticket_id, model_name)`,
deduped by issue number, newest run wins on title/url/state. Additionally the
`issues-created` payload items gain a `state` field so previously-closed issues are not
recreated as "open" in Odoo. Odoo side: stop unlinking on `failed`.

## REVA changes

### API (`api/app/`)

- `CreateIssuesRequest` (`schemas/ticket_issues.py`): + `issue_type: str | None = None`,
  validated against the 8-code set (422 otherwise). Field is optional → shipped addons
  remain compatible.
- `TicketIssueJobParams` (`reva/types.py`): + `issue_type: str | None = None`.
- Accept-time behavior otherwise unchanged (URL validation, reachability check, pending
  dedup returning the same `request_id`, stub row, enqueue).

### DB (`reva/db/`)

- Migration `023_ticket_issue_type.sql`: `ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT
  EXISTS issue_type TEXT` (nullable) + matching ORM field on `TicketIssueRun`.
- `record_ticket_issue_run_created` stores `issue_type`.
- New read helpers in `writers.py`:
  - `get_ticket_issue_union(db, instance_id, ticket_id, model_name)` → deduped
    issue-union snapshot (used by both callbacks).
  - `get_latest_ticket_issue_parent(db, instance_id, ticket_id, model_name,
    repo_full_name)` → newest non-null `parent_issue` for the ticket in this repo.
- `compute_planning_basis`: when `issue_type` is set, fold it into the basis string
  (e.g. `cr:text:<sha1>`), so the same text sent as CR vs untyped plans separately;
  untyped format stays byte-identical to today (markers/adoption unaffected).

### Worker (`worker/worker/ticket_issue_runner.py`)

- **Planner call**: unchanged resolution order (resume → adopt → reconcile → plan).
  When planning, the user prompt states the fixed type when `issue_type` is set;
  regardless, code overrides every planned type with `params.issue_type` when present.
- **Titles** (`_issue_title`): `[{type}] {ticket_id} - {tldr}` + ` ({n}/{total})` when
  total ≥ 2. Hard 30-char truncation of the tldr as backstop. Plans persisted before
  this change (issues without `type`) fall back to `DEV`.
- **Parent/epic**:
  - Before creating a parent, look up the ticket's existing parent via
    `get_latest_ticket_issue_parent`. Found → adopt it (persist into this run's
    `parent_issue`, attach children to it). Not found → create one only when the current
    request yields ≥ 2 issues.
  - New-parent title: `[{type}] {ticket_id} - {tldr}` where type = most common child
    type (tie → first child's) and tldr = truncated ticket name. No `(n/total)`.
  - Attach failures stay soft (existing `attached` flag semantics).
- **Labels**: `ensure_label` for `reva-ticket` (as today) + the type label used in this
  run (8 fixed definitions: code, color, description). `create_issue(labels=
  ["reva-ticket", <TYPE>])`.
- **`issues-created` callback**: payload `issues` becomes the union snapshot
  (`get_ticket_issue_union`), items now `{number, title, url, state}`. `failed` callback
  unchanged (`issues: []`).
- **`sync_ticket_issue_state`**: after `update_ticket_issue_state`, the per-ticket
  snapshot posted to `/issue-state` is the same union (fixes the wipe hazard).

### Prompts (`prompts/ticket_issues.md`, `reva/ticket_issue_tool.py`)

- Tool schema: each issue gains required `type` (enum of the 8 codes).
- Prompt: per-issue `type` selection guidance; `title` is the tldr — "≤ 30 characters,
  imperative, specific"; when the request fixes the type, echo it.
- Version bump + `prompts/CHANGELOG.md` entry.

### TUI (`tui/`)

- `issue_type` added to the `/api/v1/ticket-issue-runs` summary (api schema + Go
  `TicketIssueRunSummary`) and shown as a tag in the Tickets drill-down detail view.
  Titles carry `[TYPE]` anyway, so the list view needs no new column.

### Docs

- `docs/github-issue-creation.md`: new field, title convention, union-snapshot
  semantics, one-epic-per-ticket, type labels. Fix the stale marker description
  (missing `basis` component) while touching it.

## Odoo module changes (`../ast-odoo/custom_addons/cu_reva_ticket_analysis` → 19.0.9.0.0)

- **Wizard** `reva.issue.wizard` (TransientModel): `issue_type` Selection over the 8
  codes, **optional** (empty = "let REVA decide"); `text` Text required; target record
  via context. `ir.model.access` line for `base.group_user`.
- **Button** "Create Issue" on the GitHub Issues notebook page (both host views),
  visible when `reva_enabled`, `reva_github_url` set, and `reva_issue_status !=
  "pending"` (one in-flight request per record, mirroring REVA's pending dedup).
- **Send path**: refactor the request-building part of `action_create_github_issues`
  into a shared helper; the wizard sends the standard Contract-1 payload with
  `description = text` (plain), `analysis_html = ""`, `issue_type` when chosen, no
  docx. Same state transitions as the existing button: `reva_issue_status = "pending"`,
  store `request_id`.
- **Router** (`routers/reva_router.py`):
  - `IssueItem` gains optional `state` ("open"/"closed", default "open");
    `_apply_reva_issues` writes it instead of hardcoding "open".
  - `failed` no longer unlinks existing `reva.github.issue` records (status/error/chatter
    only).
  - `/issue-state` handler unchanged (REVA now sends the union snapshot).
- **Views**: issue-list visibility switches from `reva_issue_status != 'created'` to
  "no issue records" so the list stays visible while a follow-up request is pending or
  failed.
- **Tests**: wizard payload (mocked `requests`), optional-type omission, callback with
  `state` field, failed-callback no longer wipes, list visibility.
- **Docs**: update `docs/github-issues-handoff.md` (optional `issue_type` in Contract 1,
  `state` in Contract 2 items, union semantics, failure-path change).

## Compatibility / rollout

- Old addon + new REVA: fine — `issue_type` never sent (planner picks types), union
  payload's extra `state` key is ignored by the old Pydantic schema, union list is a
  superset only when older runs exist (replace semantics still correct).
- New addon + old REVA: harmless — Pydantic ignores the unknown `issue_type` request
  field, so the run degrades to an untyped one. Still, deploy REVA first, then the
  addon, so typed requests are honored from the first click.
- Existing pending runs across deploy: resumed plans without per-issue `type` fall back
  to `DEV`.

## Testing / definition of done

- REVA: `make test` (api + worker + scheduler; shared `reva/` touched), `ruff`,
  `cd tui && go build ./... && go vet ./... && go test ./...`.
- Integration-only constructs (raw migration SQL) validated via `make test-integration`
  or first staging boot.
- Addon: `uv run odoo/odoo-bin ... --test-tags cu_reva` green.
- Honest caveat: the planner-prompt change and the live GitHub/callback path are
  unit-tested with mocks; first end-to-end run creates real issues — smoke-test against
  a throwaway repo.
