# Ticket↔PR loop closure — design

**Date:** 2026-07-05
**Component:** `cu_reva` — structured-analysis persistence, issue assignee, AC-grounded reviews, `/tickets/ready` signal, digest surfaces, `/tickets/change-note` merge summaries. Touches: DB (2 columns + 1 table), `reva/` (odoo_client, types, new `reva/ticket_links.py`, new prompt), `api/` (webhook merge hook, CreateIssuesRequest), `worker/` (reviewer param, new change-note job, issue-state hook), TUI, contracts.
**Status:** Design approved (Q&A with Joseph, 2026-07-05), pending implementation plan.

## Problem

REVA pushes information forward (ticket → analysis → issues → reviews) but
nothing flows back: reviews can't see the ticket's acceptance criteria, the
consultant learns "all issues done" by checking GitHub links manually, nobody
sees per-dev ownership of created issues, and a merged PR leaves no trace on
the Odoo ticket. The original Tier-2 AC-enrichment was deferred because the
PR→ticket linkage was "lossy" — the typed-issues feature has since made it
exact (`ticket_issue_runs` maps `repo_full_name` + issue number → ticket).

## Context (verified 2026-07-05)

- `ticket_issue_runs` carries `repo_full_name`, per-issue numbers/state
  (union snapshot), `odoo_instance_id`, `ticket_id` — the resolver substrate;
  `update_ticket_issue_state` already matches incoming GitHub issue webhooks
  this way, and `issue_state` callbacks sync open/closed to Odoo.
- `ticket_analyses` persists only `result_html` — structured
  `AcceptanceCriterion`/`MissingInfoItem` objects are validated then thrown
  away.
- `stated_intent` (reviewer) already fetches closing-ref GitHub issues; the
  closing-ref regex is `_ISSUE_REF_RE` in `worker/worker/reviewer.py`.
- `mark_open_findings_at_merge` shows the webhook layer already reacts to
  merged PRs — the change-note trigger hooks the same event.
- `GitHubClient.create_issue(token, owner, repo, title, body, labels=None)`;
  GitHub's create-issue API accepts `assignees`.
- Callbacks are namespaced (`/tickets/*`); the contract-tests coverage test
  (in implementation) forces every new callback to publish a `CONTRACTS`
  entry.
- Standing decision: REVA never auto-completes tickets.

### Locked decisions

1. **Optional by construction** (Joseph: not every developer uses the issue
   system yet): every part of this feature keys off REVA-created issue links —
   no links, no behavior. Additional kill switches:
   `.claude-review.yml ticket_grounding: false` (AC param) and
   `change_notes: false` (merge notes).
2. **Assignee:** Odoo sends optional `github_username` on create-issues;
   REVA assigns it to every created issue **and the parent epic**; invalid
   username degrades (retry without + ops event), never fails creation.
   Purpose: per-dev ownership visibility.
3. **Ready signal:** when a close transition leaves zero open issues in the
   ticket's union (≥1 issue exists), POST `{base}/tickets/ready` — Odoo marks/
   notifies the consultant. Transition-triggered; reopen→re-close fires again
   (Odoo handles idempotently). Informs only — never completes.
4. **Change note:** on PR merged with closing refs resolving to REVA issues,
   POST `{base}/tickets/change-note` with a consultant-facing summary in the
   ticket's language (German for most customers) as an **internal note** —
   what changed functionally, which modules, what to verify when testing.
5. The original "customer-reply drafting" feature was **dropped** (consultants
   usually already know the missing-info answers — the analysis is their
   checklist, not an outbox).

### Explicitly out of scope

- Auto-completing tickets (standing decision), customer-visible messages.
- Multiple assignees / GitHub team assignment (single optional username).
- Deployment tracking beyond "all issues closed" (odoo.sh state is invisible
  to REVA).
- TUI quality dashboard (separately deferred).

## Design

### 1. Structured analysis persistence

`ticket_analyses.result_structured JSONB` (nullable; migration takes the next
free number) + ORM field. `record_ticket_analysis_completed` writes
`result.model_dump()` alongside the HTML. Retention: kept when raw
`input_text` is purged (derived content, like `result_html`). Reader:
`writers.get_latest_structured_analysis(db, odoo_instance_id, ticket_id,
model_name) -> dict | None` (newest completed row with a structured result).

### 2. Issue assignee

- `CreateIssuesRequest.github_username: str | None = None` (optional additive
  field — allowed by the shipped-addon contract note); flows through
  `TicketIssueJobParams` onto a new `ticket_issue_runs.github_username`
  column.
- `GitHubClient.create_issue(..., assignees: list[str] | None = None)`;
  runner passes `[github_username]` for every child issue + the parent epic.
  A 422 naming the assignee → retried once without assignees + ops event
  (`component="github"`, `event="assignee_rejected"`).
- TUI tickets detail shows the assignee; contracts sample gains the field.

### 3. Shared resolver — `reva/ticket_links.py`

`resolve_pr_tickets(db, repo_full_name, closing_issue_numbers) ->
list[TicketRef]` with `TicketRef(odoo_instance_id, ticket_id, model_name,
run_id)` — the one lookup 4 and 6 both use (and the reviewer indirectly):
issue numbers → `ticket_issue_runs` rows for that repo → deduped tickets.
Pure DB, no network.

### 4. AC-grounded reviews (`ticket_acceptance_criteria` param)

In the reviewer, where `stated_intent` resolves closing refs: additionally
resolve refs → tickets (via §3) → `get_latest_structured_analysis` → build a
fenced `ticket_acceptance_criteria` param (own nonce, UNTRUSTED framing —
derived from customer text): ticket summary + AC list (given/when/then +
confidence). Guidance (review_guidance.md + CHANGELOG bump): check the diff
against the ACs; contradictions → ordinary `bug` findings, unimplemented ACs
→ `maintainability`, advisory confidence rules as with stated_intent. Kill
switch `ticket_grounding: false`; no links or no structured analysis → param
omitted (prompt-cache stable). Degradations → ops event.

### 5. Ready signal + digest

- Hook in the issue-state sync path (worker): after updating the union, if
  the transition closed the last open issue → `odoo.tickets_ready(ticket_id,
  model_name, issues=union)` → `POST {base}/tickets/ready`. Runs AFTER the
  existing `issue_state` callback. Error semantics: **TransientError
  re-raises** (RQ retries the whole state-sync job — it is idempotent, and a
  swallowed transient would silently lose the ready signal until a future
  reopen→close); **PermanentError is swallowed** + logged + ops event (a 4xx
  from Odoo won't improve on retry, and state sync itself succeeded).
- Digest surfaces: TUI tickets tab row indicator (✔ ready) using data the tab
  already loads; dashboard counter `tickets_ready_14d` on
  `/api/v1/metrics/dashboard`; weekly Chat report gains a "Ready for
  deployment" section (repo, ticket, closed-issue count, since-when).

### 6. Merge change note

- Webhook: on `pull_request` closed with `merged=true`, after existing
  merge handling → parse closing refs from the PR body (`_ISSUE_REF_RE`) →
  if any, enqueue `worker.change_note_tasks.run_change_note`
  (`{repo, pr_number, head/merge info}`; retry policy like ticket jobs).
- Job: resolve tickets (§3); none → done (the optionality). Per ticket:
  dedup via new `change_notes` table (`repo_full_name, pr_number, ticket_id,
  odoo_instance_id, model_name, status, note_html, estimated_cost_usd,
  created_at`; unique `(repo_full_name, pr_number, ticket_id)` — retries and
  re-deliveries safe). Budget-gated (`budget_exceeded`). Fetch the PR diff
  (existing GitHub diff machinery; oversized → title/body/file-list
  fallback), one Messages call (default model, forced tool, new
  `prompts/change_note.md`): consultant-facing summary in the ticket's
  language (language cue: the run's ticket `name`), sections *what changed /
  affected areas / what to verify*. Fenced inputs (diff + PR text are
  untrusted). Then `odoo.change_note(ticket_id, model_name, pr={number,
  title, url, repo}, note_html)` → `POST {base}/tickets/change-note`; persist
  row + spend (`kind="change_note"`).
- Idempotent resume: completed `change_notes` row with `note_html` but failed
  callback → retry re-posts without re-paying (the ticket-analysis pattern).

### Contracts & coordination

New `CONTRACTS` entries: `tickets.ready`, `tickets.change-note`; updated
`create-issues` sample (+`github_username`). The coverage drift test enforces
this. Prompt CHANGELOG bump for `change_note.md` + the guidance section
(coordinate with triage/scanner bumps). Migration numbers: next free at
implementation time.

## Error handling summary

| Case | Behavior |
|---|---|
| PR has no closing refs / refs aren't REVA issues | nothing happens anywhere (by design) |
| `github_username` invalid | issues created unassigned + ops event |
| No structured analysis for a linked ticket | AC param omitted; debug log |
| Ready callback fails | state sync unaffected; RQ retry semantics per error class + ops event |
| Change-note Claude/permanent failure | `change_notes.status="failed"` + ops event; no Odoo note |
| Duplicate merge delivery | unique constraint → resume/skip |
| Budget cap | change note skipped (`status="skipped_budget"`) + ops event |

## Testing

Writers/migrations (structured persistence, `change_notes` dedup, assignee
column); resolver on seeded runs; reviewer param wiring matrix (linked/
unlinked/killed/missing-analysis); ready-transition matrix (last-close fires,
partial close doesn't, reopen→re-close refires); change-note job with fakes
(dedup, budget, fallback on oversized diff, language cue, idempotent resume,
callback error mapping); assignee retry-without on 422; contracts round-trips
for both new payloads; TUI mock rendering. Staging gates: one real
ticket→issues→PR cycle end-to-end (assigned issues visible, ready fires once,
note lands as internal note in the right language).
