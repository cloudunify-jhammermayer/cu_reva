# Ticket journey view (TUI) — design

**Date:** 2026-07-10
**Component:** `cu_reva` — read-only cross-system aggregation. Touches: new
`api/app/routes/v1/ticket_journeys.py` (+ query + schema), `tui/`
(`internal/api/{types,client,iface,mock}.go`, `internal/ui/tickets.go`).
No DB writes, no migrations, no Claude calls.
**Status:** Design approved (brainstorm Q&A with Joseph, 2026-07-10 — TUI
only, no docs-ui), pending implementation plan.

## Problem

Answering "what did REVA do for this ticket?" today means reading four TUI
tabs and the DB: analysis in tickets, issues in ticket-issues, reviews in
reviews, change notes nowhere visible. There is no single timeline from
customer ticket to merged code — for debugging, oversight, or demonstrating
value.

## Context (verified 2026-07-10)

Everything needed is already persisted: `ticket_analyses` (requested/
completed, model, cost), `ticket_issue_runs` (issues union with state,
`plan_date`, `complete_date`, `estimate_hours`, `github_project_url`),
`change_notes` (repo, pr_number, ticket, status, language, cost),
`review_runs` + `pull_requests` (risk, finding count, status, joinable by
repo + PR number). The TUI tickets tab (`internal/ui/tickets.go`) already has
a detail pane keyed by (odoo_instance_id, model_name, ticket_id).

**Known linkage gap (stated honestly):** pre-merge PR↔ticket linkage is not
persisted — closing refs are parsed in-flight by the reviewer and change-note
job only. In v1, reviews enter the journey two ways: (a) reviews of PRs that
later produced a `change_notes` row for this ticket (merged work), and
(b) once the issue-conformance feature (spec 2026-07-10) ships, reviews whose
persisted `intent_check` cites one of the ticket's issue numbers. Reviews of
open PRs without either signal are invisible to the journey; closing that gap
(persisting resolved refs per review) is future work, not v1.

## Design

### Endpoint

`GET /api/v1/ticket-journeys?odoo_instance_id=&model_name=&ticket_id=`
(Bearer `REVA_API_KEY`, same auth as the rest of `/api/v1`). Returns one
journey:

```json
{
  "ticket": {"odoo_instance_id": 1, "model_name": "helpdesk.ticket",
              "ticket_id": 4711, "ready": true},
  "events": [
    {"ts": "...", "kind": "analysis_completed",
     "summary": "Analysis v2 (claude-sonnet-5), $0.31"},
    {"ts": "...", "kind": "issues_created",
     "summary": "5 issues (+epic), 11.5h estimated, board: <project url>"},
    {"ts": "...", "kind": "review_completed",
     "summary": "acme/widgets#88 diff review — risk low, 2 findings"},
    {"ts": "...", "kind": "issue_closed", "summary": "#42 Export button"},
    {"ts": "...", "kind": "change_note_posted",
     "summary": "acme/widgets#88 → internal note (de)"},
    {"ts": "...", "kind": "ready",
     "summary": "All 5 issues closed"}
  ]
}
```

Assembly is one read-only query module (`api/app/queries/ticket_journeys.py`):

- `analysis_requested` / `analysis_completed` / `analysis_failed` — from
  `ticket_analyses` rows (all runs, not just latest).
- `issues_created` — per `ticket_issue_runs` run: issue count, epic, summed
  `estimate_hours`, project URL.
- `issue_closed` — derived per union item from `complete_date` (state-change
  history is not persisted; a reopened issue simply loses the event on the
  next snapshot — acceptable for an ops view, noted in the pane footer).
- `review_completed` — `review_runs` joined via `pull_requests` for PRs
  linked per the two linkage signals above; dedup by run id.
- `change_note_posted` — `change_notes` rows (status, language).
- `ready` — computed (≥1 issue and all closed), timestamped with the max
  `complete_date`; matches the tickets tab's existing ✔-ready semantics.

Events sorted by `ts`; unknown/missing timestamps sort last with `ts: null`.
404 when the ticket has neither analyses nor issue runs.

### TUI

Tickets tab detail gains a **Journey** section: one line per event —
`<date> <kind symbol> <summary>` — reusing the tab's existing key context and
fetch pattern (`internal/api/client.go` + `iface` + `mock` with demo events;
`tickets_test.go` extended). Long journeys truncate to the most recent ~30
events with a "(+N earlier)" head line.

## Error handling

| Case | Behavior |
|---|---|
| Ticket unknown | 404 |
| Partial data (e.g. analysis only) | Journey renders what exists — every source is independent |
| Review linkage absent (open PR, no intent_check) | Review not shown (documented v1 gap) |
| Cross-instance ticket id collision | Keyed by (odoo_instance_id, model_name, ticket_id) throughout — no fuzzy matching |

## Testing

- Unit (api): journey assembly matrix on seeded SQLite — analyses only /
  +issues / +closed issues / +change note + linked review / intent_check
  linkage / ready computation / ordering / 404; auth required.
- TUI: mock journey rendering incl. truncation; `go build ./... && go vet
  ./... && go test ./...`.
- No integration gate needed (read-only over existing tables; Postgres-only
  constructs avoided — plain ORM selects).

## Out of scope

- docs-ui / consultant-facing surface (decision 2026-07-10: TUI only).
- Persisting new events or state-change history; pre-merge PR↔ticket
  linkage persistence (future work, unblocks fuller review coverage).
- Cost/value aggregation (the value report owns that).
