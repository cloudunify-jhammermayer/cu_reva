# Ticket Journey View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One read-only timeline per Odoo ticket — analysis → issues created → issue closes → linked reviews → change notes → ready — served by a new `/api/v1/ticket-journeys` endpoint and rendered in the TUI tickets detail pane.

**Spec:** `docs/superpowers/specs/2026-07-10-ticket-journey-view-design.md` (approved 2026-07-10; TUI only, no docs-ui, by explicit decision).

**Architecture:** Pure aggregation over existing tables (`ticket_analyses`, `ticket_issue_runs` + `get_ticket_issue_union`, `change_notes`, `review_runs`⋈`pull_requests`⋈`repositories`) — no DB writes, no migrations, no Claude calls. Review linkage uses the two persisted signals: change-note rows (merged PRs) and `review_runs.intent_check` issue refs (from the conformance feature); reviews of open PRs without either signal are invisible — the spec's documented v1 gap. The TUI keys the fetch by (odoo_instance_id, model_name, ticket_id), so Task 1 first exposes `odoo_instance_id` on the two ticket API summaries (it's currently not surfaced).

**Planning deviation from the spec, with reason:** the TUI detail pane only opens for rows that have a create-issues run (`cur.row.issueRun != nil` in tickets.go) — analysis-only tickets have no detail view today. The journey pane rides the existing detail view, so analysis-only tickets get no journey pane in v1 even though the endpoint supports them. Widening the detail-open condition is out of scope (it would restructure the tab); Task 5 records this in the spec.

**Tech Stack:** Python 3.14 (FastAPI, SQLAlchemy, pydantic), Go/Bubble Tea TUI.

## Global Constraints

- Keyed strictly by `(odoo_instance_id, model_name, ticket_id)` — no fuzzy matching. `odoo_instance_id` is an optional query param; omitted/None matches legacy NULL-instance rows (the `_instance_filter(None) → .is_(None)` semantics already used by `get_ticket_issue_union`).
- Event payload: `{ts: datetime|null, kind: str, summary: str}`; events sorted ascending by ts, null-ts events LAST. Kinds (exact strings): `analysis_requested`, `analysis_completed`, `analysis_failed`, `issues_created`, `issue_closed`, `review_completed`, `change_note_posted`, `ready`.
- 404 when the ticket has neither analyses nor issue runs; partial data renders what exists (every source independent).
- `ready` = ≥1 union issue and all closed (matches the tickets tab's existing ✔-ready semantics); its ts = the max `complete_date`, null if none parseable.
- Read-only: no new tables/columns; portable ORM only (no Postgres-only constructs — `intent_check` ref matching happens in Python over rows already filtered to the ticket's repos).
- TUI journey truncation: at most the 30 most recent events, with a `(+N earlier)` head line when truncated.
- Auth: master gate (`require_api_key`), registered in `api/app/routes/v1/__init__.py`'s `_master` router like every other read route.
- Gates: `make test` (all three services — shared `reva/` is NOT touched, but run all anyway per DoD), `ruff`, mypy no-new, `cd tui && go build ./... && go vet ./... && go test ./...`, `gofmt -l tui/` empty.
- Per-service venvs: `cd api && .venv/bin/python -m pytest tests/...`.

---

### Task 1: Expose `odoo_instance_id` on the ticket API summaries

**Files:**
- Modify: `api/app/queries/ticket_analyses.py` (list dict), `api/app/queries/ticket_issues.py` (list dict)
- Modify: `api/app/schemas/ticket_analyses.py`, `api/app/schemas/ticket_issues.py` (summary models)
- Test: `api/tests/test_v1_ticket_analyses.py`, `api/tests/test_v1_ticket_issues.py`

**Interfaces:**
- Produces: `odoo_instance_id: int | null` on `TicketAnalysisSummary` and `TicketIssueRunSummary` JSON — Task 3's Go types and Task 4's fetch keying consume it.

- [ ] **Step 1: Write the failing tests**

In each of the two api test files, extend an existing list test (or add one, mirroring the file's seeding — the seeded rows carry `odoo_instance_id=1` or similar; read the seeding helper first):

```python
def test_list_items_include_odoo_instance_id(client_and_db):
    ...seed one row the way the file's list tests do...
    resp = client.get("/api/v1/ticket-analyses")   # or /ticket-issue-runs
    assert resp.status_code == 200
    assert resp.json()["items"][0]["odoo_instance_id"] == <the seeded instance id>
```

(The assertion is the requirement; mirror the neighboring tests' seeding exactly. Seed one row with a non-null instance id; if the file also seeds legacy null-instance rows, assert `is None` for those.)

- [ ] **Step 2: Run to verify they fail**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_ticket_analyses.py tests/test_v1_ticket_issues.py -k odoo_instance -v`
Expected: FAIL — `KeyError: 'odoo_instance_id'`

- [ ] **Step 3: Implement**

Add `"odoo_instance_id": r.odoo_instance_id,` to the per-item dict in both query modules, and `odoo_instance_id: int | None = None` to both summary schema classes (next to `ticket_id`/`model_name`).

- [ ] **Step 4: Run both suites**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_ticket_analyses.py tests/test_v1_ticket_issues.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add api/app/queries/ticket_analyses.py api/app/queries/ticket_issues.py api/app/schemas/ticket_analyses.py api/app/schemas/ticket_issues.py api/tests/test_v1_ticket_analyses.py api/tests/test_v1_ticket_issues.py
git commit -m "feat(api): expose odoo_instance_id on ticket analysis/issue-run summaries"
```

---

### Task 2: Journey aggregation query + schema + route

**Files:**
- Create: `api/app/queries/ticket_journeys.py`
- Create: `api/app/schemas/ticket_journeys.py`
- Create: `api/app/routes/v1/ticket_journeys.py`
- Modify: `api/app/routes/v1/__init__.py` (import + `_master.include_router(ticket_journeys.router)`)
- Test: `api/tests/test_v1_ticket_journeys.py` (new)

**Interfaces:**
- Consumes: `reva.db.writers.get_ticket_issue_union(db, odoo_instance_id, ticket_id, model_name)` (existing; items carry `number,title,state,complete_date,...`); ORM models `TicketAnalysis`, `TicketIssueRun`, `ChangeNote`, `ReviewRun`, `PullRequest`, `Repository`.
- Produces: `GET /api/v1/ticket-journeys?model_name=&ticket_id=&odoo_instance_id=` → `{ticket: {odoo_instance_id, model_name, ticket_id, ready}, events: [{ts, kind, summary}]}` (404 as specced). Task 3's Go client consumes this shape.

- [ ] **Step 1: Write the failing tests**

Create `api/tests/test_v1_ticket_journeys.py`. Use the shared `client_and_db` fixture (same as the other v1 test files) and seed via `reva.db.writers` / direct ORM adds, mirroring the seeding idioms in `test_v1_ticket_analyses.py` (analyses), `test_v1_ticket_issues.py` (issue runs), and `test_v1_reviews.py` (repo+PR+review). The matrix (each test seeds only what it names):

```python
def test_journey_404_for_unknown_ticket(client_and_db):
    client, _ = client_and_db
    resp = client.get("/api/v1/ticket-journeys?model_name=helpdesk.ticket&ticket_id=1&odoo_instance_id=1")
    assert resp.status_code == 404


def test_journey_analyses_only(client_and_db):
    # Seed one completed analysis (instance 1, ticket 4711).
    # Expect: analysis_requested then analysis_completed, ready False, no 404.
    ...
    data = resp.json()
    kinds = [e["kind"] for e in data["events"]]
    assert kinds == ["analysis_requested", "analysis_completed"]
    assert data["ticket"]["ready"] is False


def test_journey_failed_analysis_event(client_and_db):
    # Seed a failed analysis -> analysis_requested + analysis_failed (summary carries the error, truncated).
    ...


def test_journey_issues_and_closes_and_ready(client_and_db):
    # Seed a completed issue run: 2 issues, both closed with complete_date
    # "2026-07-08"/"2026-07-09", estimate_hours 1.5 each.
    # Expect kinds: issues_created, issue_closed, issue_closed, ready;
    # issues_created summary mentions "2 issues" and "3.0h";
    # ready ts date == 2026-07-09; ticket.ready True.
    ...


def test_journey_change_note_links_review(client_and_db):
    # Seed: issue run (repo acme/widgets) + a completed change_notes row for
    # (repo, pr 88) + repo/PR/completed review_run rows for acme/widgets#88.
    # Expect: review_completed AND change_note_posted events present;
    # review summary mentions "acme/widgets#88".
    ...


def test_journey_intent_check_links_review(client_and_db):
    # Seed: issue run whose union has issue number 42 (repo acme/widgets) +
    # a completed review_run for acme/widgets PR 90 whose intent_check is
    # [{"issue_number": 42, "verdict": "matches", "note": "ok"}] — NO change note.
    # Expect: exactly one review_completed event.
    # Also seed a review for the same repo citing UNRELATED issue 999 →
    # must NOT appear.
    ...


def test_journey_orders_by_ts_nulls_last(client_and_db):
    # Seed events with mixed timestamps (an analysis without completed_at
    # yields only analysis_requested; a ready with no parseable complete_date
    # yields ts null) — assert ascending order and null-ts events at the end.
    ...


def test_journey_instance_scoping(client_and_db):
    # Same (model_name, ticket_id) on instance 1 and instance 2 — querying
    # instance 1 must not see instance 2's analyses/runs.
    ...


def test_journey_requires_master_key(client_and_db):
    # Same auth idiom as the other v1 read routes (mirror test_auth.py /
    # neighboring files): missing/wrong bearer -> 401/403.
    ...
```

(Each `...` = seeding + GET, mirroring the named neighbor files; the assertions shown are the requirement.)

- [ ] **Step 2: Run to verify they fail**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_ticket_journeys.py -v`
Expected: FAIL — 404 route not found (`/ticket-journeys` unregistered)

- [ ] **Step 3: Implement the schema**

`api/app/schemas/ticket_journeys.py`:

```python
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class JourneyEvent(BaseModel):
    ts: datetime | None
    kind: str
    summary: str


class JourneyTicket(BaseModel):
    odoo_instance_id: int | None
    model_name: str
    ticket_id: int
    ready: bool


class TicketJourney(BaseModel):
    ticket: JourneyTicket
    events: list[JourneyEvent]
```

- [ ] **Step 4: Implement the query module**

`api/app/queries/ticket_journeys.py` — the assembly logic (complete; adjust imports to the repo's actual module layout, matching the other query modules):

```python
"""Ticket journey — read-only timeline over existing tables (spec 2026-07-10).

Review linkage is the documented v1 gap: reviews enter via change-note rows
(merged PRs) or persisted intent_check issue refs; open PRs without either
signal are invisible here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from reva.db import writers
from reva.db.engine import Database
from reva.db.models import (
    ChangeNote,
    PullRequest,
    Repository,
    ReviewRun,
    TicketAnalysis,
    TicketIssueRun,
)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def get_ticket_journey(
    db: Database, odoo_instance_id: int | None, model_name: str, ticket_id: int
) -> dict | None:
    events: list[dict] = []

    def _instance(col):
        return col.is_(None) if odoo_instance_id is None else col == odoo_instance_id

    with db.session() as s:
        analyses = s.execute(
            select(TicketAnalysis).where(
                TicketAnalysis.ticket_id == ticket_id,
                TicketAnalysis.model_name == model_name,
                _instance(TicketAnalysis.odoo_instance_id),
            )
        ).scalars().all()
        runs = s.execute(
            select(TicketIssueRun).where(
                TicketIssueRun.ticket_id == ticket_id,
                TicketIssueRun.model_name == model_name,
                _instance(TicketIssueRun.odoo_instance_id),
            )
        ).scalars().all()
        if not analyses and not runs:
            return None

        for a in analyses:
            events.append({"ts": a.created_at, "kind": "analysis_requested",
                           "summary": f"Analysis requested ({a.field_name})"})
            if a.status == "completed":
                cost = f", ${float(a.estimated_cost_usd):.2f}" if a.estimated_cost_usd else ""
                events.append({"ts": a.completed_at, "kind": "analysis_completed",
                               "summary": f"Analysis completed ({a.model or 'unknown model'}{cost})"})
            elif a.status == "failed":
                events.append({"ts": a.completed_at or a.created_at, "kind": "analysis_failed",
                               "summary": f"Analysis failed: {(a.error_message or 'unknown error')[:120]}"})

        repos: set[str] = set()
        for r in runs:
            if r.repo_full_name:
                repos.add(r.repo_full_name)
            if r.status == "completed" and r.issues:
                n = len(r.issues)
                total = sum(i.get("estimate_hours") or 0 for i in r.issues)
                bits = [f"{n} issue{'s' if n != 1 else ''}"]
                if r.parent_issue:
                    bits.append("+epic")
                if total:
                    bits.append(f"{total:.1f}h estimated")
                if r.github_project_url:
                    bits.append(f"board: {r.github_project_url}")
                events.append({"ts": r.created_at, "kind": "issues_created",
                               "summary": ", ".join(bits)})

        union = writers.get_ticket_issue_union(db, odoo_instance_id, ticket_id, model_name)
        union_numbers = {i["number"] for i in union}
        for item in union:
            if item.get("complete_date"):
                events.append({"ts": _parse_date(item["complete_date"]), "kind": "issue_closed",
                               "summary": f"#{item['number']} {item['title']} closed"})

        notes = s.execute(
            select(ChangeNote).where(
                ChangeNote.ticket_id == ticket_id,
                ChangeNote.model_name == model_name,
                _instance(ChangeNote.odoo_instance_id),
            )
        ).scalars().all()
        note_pairs = set()
        for cn in notes:
            note_pairs.add((cn.repo_full_name, cn.pr_number))
            events.append({"ts": cn.completed_at or cn.created_at, "kind": "change_note_posted",
                           "summary": f"{cn.repo_full_name}#{cn.pr_number} → internal note ({cn.status})"})

        seen_reviews: set[int] = set()
        if repos or note_pairs:
            review_rows = s.execute(
                select(ReviewRun, Repository.full_name, PullRequest.pr_number)
                .join(Repository, ReviewRun.repository_id == Repository.id)
                .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
                .where(
                    Repository.full_name.in_(repos | {p[0] for p in note_pairs}),
                    ReviewRun.status == "completed",
                )
            ).all()
            for rr, repo_name, pr_number in review_rows:
                linked = (repo_name, pr_number) in note_pairs
                if not linked and rr.intent_check:
                    linked = any(
                        v.get("issue_number") in union_numbers for v in rr.intent_check
                    )
                if not linked or rr.id in seen_reviews:
                    continue
                seen_reviews.add(rr.id)
                events.append({
                    "ts": rr.completed_at or rr.created_at, "kind": "review_completed",
                    "summary": f"{repo_name}#{pr_number} {rr.review_mode} review — "
                               f"risk {rr.risk_level or '?'}, {rr.finding_count} finding"
                               f"{'s' if rr.finding_count != 1 else ''}",
                })

    ready = bool(union) and all(i.get("state") == "closed" for i in union)
    if ready:
        closes = [_parse_date(i.get("complete_date")) for i in union]
        closes = [c for c in closes if c is not None]
        events.append({"ts": max(closes) if closes else None, "kind": "ready",
                       "summary": f"All {len(union)} issues closed"})

    def _sort_key(e: dict):
        ts = e["ts"]
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (ts is None, ts or datetime.max.replace(tzinfo=timezone.utc))

    events.sort(key=_sort_key)
    return {
        "ticket": {"odoo_instance_id": odoo_instance_id, "model_name": model_name,
                   "ticket_id": ticket_id, "ready": ready},
        "events": events,
    }
```

(SQLite tests may yield naive datetimes — the sort key normalizes; if pydantic serialization needs it, keep ts values as-is otherwise.)

- [ ] **Step 5: Implement the route + registration**

`api/app/routes/v1/ticket_journeys.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_db
from app.queries import ticket_journeys as q
from app.schemas.ticket_journeys import TicketJourney
from reva.db.engine import Database

router = APIRouter()


@router.get("/ticket-journeys", response_model=TicketJourney)
def get_ticket_journey(
    model_name: str,
    ticket_id: int,
    odoo_instance_id: int | None = None,
    db: Database = Depends(get_db),
) -> TicketJourney:
    data = q.get_ticket_journey(db, odoo_instance_id, model_name, ticket_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Ticket has no REVA activity")
    return TicketJourney.model_validate(data)
```

In `api/app/routes/v1/__init__.py`: add `ticket_journeys` to the import list and `_master.include_router(ticket_journeys.router)` next to the other ticket routers.

- [ ] **Step 6: Run the suite**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_ticket_journeys.py -v && cd .. && cd api && .venv/bin/python -m pytest tests/ -q`
Expected: new tests PASS; full api suite PASS.

- [ ] **Step 7: Commit**

```bash
git add api/app/queries/ticket_journeys.py api/app/schemas/ticket_journeys.py api/app/routes/v1/ticket_journeys.py api/app/routes/v1/__init__.py api/tests/test_v1_ticket_journeys.py
git commit -m "feat(api): ticket-journeys endpoint — read-only per-ticket timeline"
```

---

### Task 3: TUI client layer (types, iface, client, mock)

**Files:**
- Modify: `tui/internal/api/types.go` (journey types + `OdooInstanceID` on the two ticket summaries)
- Modify: `tui/internal/api/iface.go`, `tui/internal/api/client.go` (new method — mirror the existing GET-with-query idiom)
- Modify: `tui/internal/api/mock.go` (journey sample + instance ids on ticket mocks)

**Interfaces:**
- Consumes: Task 2's JSON shape; Task 1's `odoo_instance_id` fields.
- Produces: `TicketJourney(odooInstanceID *int, modelName string, ticketID int) (*TicketJourney, error)` on the client interface — Task 4 calls it.

- [ ] **Step 1: Types**

`tui/internal/api/types.go`:

```go
type JourneyEvent struct {
	TS      *time.Time `json:"ts"`
	Kind    string     `json:"kind"`
	Summary string     `json:"summary"`
}

type JourneyTicket struct {
	OdooInstanceID *int   `json:"odoo_instance_id"`
	ModelName      string `json:"model_name"`
	TicketID       int    `json:"ticket_id"`
	Ready          bool   `json:"ready"`
}

type TicketJourney struct {
	Ticket JourneyTicket  `json:"ticket"`
	Events []JourneyEvent `json:"events"`
}
```

Add `OdooInstanceID *int \`json:"odoo_instance_id"\`` to `TicketAnalysisSummary` and `TicketIssueRunSummary`.

- [ ] **Step 2: Interface + client + mock**

Add `TicketJourney(odooInstanceID *int, modelName string, ticketID int) (*TicketJourney, error)` to `iface.go`; implement in `client.go` mirroring the existing query-param GET idiom (URL-escape `model_name`); in `mock.go`, return a plausible 6-event journey (analysis_requested/completed → issues_created → review_completed → issue_closed → ready) for the mocked ticket and set `OdooInstanceID` on the existing ticket mocks.

- [ ] **Step 3: Gate + commit**

Run: `cd tui && go build ./... && go vet ./... && go test ./... && gofmt -l .`
Expected: green, gofmt silent.

```bash
git add tui/internal/api/types.go tui/internal/api/iface.go tui/internal/api/client.go tui/internal/api/mock.go
git commit -m "feat(tui): ticket-journey client method + types"
```

---

### Task 4: TUI tickets detail — journey pane

**Files:**
- Modify: `tui/internal/ui/messages.go` (new msg type), `tui/internal/ui/tickets.go` (fetch on detail-open + render), `tui/internal/ui/styles.go` (`journeySymbol`)
- Test: `tui/internal/ui/tickets_test.go`

**Interfaces:**
- Consumes: Task 3's client method and types.

- [ ] **Step 1: Message type** (`messages.go`, mirroring `reviewDetailLoadedMsg`'s staleness-guard pattern):

```go
type ticketJourneyLoadedMsg struct {
	key  string // issueRunKey(model, ticket) — guards against stale responses
	data *api.TicketJourney
	err  error
}
```

- [ ] **Step 2: Fetch on detail-open** (`tickets.go`): where the `enter` case sets `t.detail = true` (using `cur.row.issueRun`), also reset `t.journey = nil; t.journeyErr = ""`, remember `t.detailKey = issueRunKey(run.ModelName, run.TicketID)`, and return a `tea.Cmd` that calls `t.client.TicketJourney(run.OdooInstanceID, run.ModelName, run.TicketID)` and wraps the result in `ticketJourneyLoadedMsg{key: ...}`. Handle the msg in `Update`: ignore when `msg.key != t.detailKey`; store `t.journey = msg.data.Events` / `t.journeyErr`. Mirror how `reviews.go` fires and consumes its detail fetch (`reviews.go:55` + the `reviewDetailLoadedMsg` handler).

- [ ] **Step 3: Symbol helper** (`styles.go`, after `intentSymbol`):

```go
func journeySymbol(kind string) string {
	switch kind {
	case "analysis_completed", "issues_created", "review_completed", "change_note_posted":
		return styleStatusCompleted.Render("+")
	case "issue_closed", "ready":
		return styleStatusCompleted.Render("✓")
	case "analysis_failed":
		return styleStatusFailed.Render("x")
	default: // analysis_requested and future kinds
		return styleStatusOther.Render("·")
	}
}
```

- [ ] **Step 4: Render** in the detail body, after the existing issues list block:

```go
	// Journey (read-only timeline; most recent 30)
	if t.journeyErr != "" {
		b.WriteString("\n" + styleSubtitle.Render("Journey unavailable: "+t.journeyErr) + "\n")
	} else if len(t.journey) > 0 {
		b.WriteString("\n" + styleTitle.Render("Journey") + "\n")
		events := t.journey
		if len(events) > 30 {
			b.WriteString(styleSubtitle.Render(fmt.Sprintf("  (+%d earlier)", len(events)-30)) + "\n")
			events = events[len(events)-30:]
		}
		for _, e := range events {
			ts := "          "
			if e.TS != nil {
				ts = e.TS.Local().Format("2006-01-02")
			}
			b.WriteString(fmt.Sprintf("  %s  %s %s\n",
				styleSubtitle.Render(ts), journeySymbol(e.Kind), truncate(e.Summary, w-18)))
		}
	}
```

(Adapt the field/state names to the tab's actual struct — `t.journey []api.JourneyEvent`, `t.journeyErr string`, `t.detailKey string` are new fields on the tickets model.)

- [ ] **Step 5: Tests** (`tickets_test.go`, mirroring the file's existing render/update test style): (a) journey msg with matching key populates and renders the "Journey" title + an event summary; (b) stale key ignored; (c) >30 events renders `(+N earlier)` and only the last 30; (d) journey error renders the unavailable line.

- [ ] **Step 6: Gate + commit**

Run: `cd tui && go build ./... && go vet ./... && go test ./... && gofmt -l .`
Expected: green, gofmt silent.

```bash
git add tui/internal/ui/messages.go tui/internal/ui/tickets.go tui/internal/ui/styles.go tui/internal/ui/tickets_test.go
git commit -m "feat(tui): journey timeline in the tickets detail pane"
```

---

### Task 5: Verification sweep + docs sync

**Files:**
- Modify: `docs/superpowers/specs/2026-07-10-ticket-journey-view-design.md` (Status line + TUI-scope note)

- [ ] **Step 1: Full gates**

Run: `make test` → all three services green (worker/scheduler untouched — must stay green).
Run: `ruff check reva worker/worker api/app scheduler/scheduler` → clean.
Run: `mypy reva worker/worker api/app scheduler/scheduler --ignore-missing-imports` → no NEW errors vs. main.
Run: `cd tui && go build ./... && go vet ./... && go test ./... && gofmt -l .` → green/silent.

- [ ] **Step 2: Spec sync**

Replace the Status line with:
`**Status:** Approved (Joseph, 2026-07-10) — implemented; see plans/2026-07-10-ticket-journey-view.md.`
Add to the TUI section (as-built note): the journey pane rides the existing tickets detail view, which opens only for rows with a create-issues run — analysis-only tickets are served by the endpoint but have no TUI pane in v1.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-07-10-ticket-journey-view-design.md
git commit -m "docs(specs): ticket journey view — mark implemented, record TUI-scope note"
```

**Honest-status note for the final report:** unit-level only (SQLite + mock client). The endpoint's behavior over real Postgres data volumes and the TUI against a live API are exercised only on staging; the review-linkage gap (open PRs without change notes or intent_check refs) is by design and documented in the spec.
