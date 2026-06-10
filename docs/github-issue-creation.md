# GitHub Issue Creation from Odoo Tickets

REVA's side of the **github-issues handoff** (`ast-odoo/custom_addons/cu_reva_ticket_analysis/docs/github-issues-handoff.md`, module 19.0.2.0.0). An Odoo user clicks **Create Issues** on a Helpdesk ticket / Project task; REVA plans 1–10 GitHub issues with Claude, creates them in the project's repository, and reports back so Odoo renders the links. Freelancers without Odoo access work from those issues.

Implemented 2026-06-10. The Odoo side was already shipped and tested — REVA conforms to it, not the other way around.

## Flow

```
Odoo (button)                REVA api                REVA worker                GitHub        Odoo (callback)
     │ POST /api/v1/create-issues │                        │                       │                │
     │──────────────────────────▶│ row in ticket_issue_runs│                       │                │
     │◀──202 {request_id: row id}│──enqueue RQ job────────▶│                       │                │
     │                            │                        │ marker search ────────▶│               │
     │                            │                        │ (reconcile? else)      │               │
     │                            │                        │ Claude plan → persist  │               │
     │                            │                        │ create issues ────────▶│               │
     │                            │                        │ persist completed      │               │
     │                            │                        │──POST /api/reva/issues-created────────▶│
     │                            │                        │   {request_id, status, issues[]}       │
```

## Contract conformance

**Contract 1 — `POST /api/v1/create-issues`** (`api/app/routes/v1/ticket_issues.py`)
- Request: exactly `{ticket_id, model_name, github_url, name, description, analysis_html, priority, ticket_url}` (`api/app/schemas/ticket_issues.py`). Do **not** add required fields — the Odoo payload is fixed.
- `github_url` is validated here (`reva/github_urls.py`; tolerates trailing `/` and `.git`) → `422` on anything that isn't `https://github.com/{owner}/{repo}`. Odoo shows the error and rolls its record back — fail fast beats an async failure.
- Response: `202 {"request_id": <ticket_issue_runs.id>, "job_id", "status"}`. Odoo stores `request_id` and the callback must echo it. The row id is a BIGSERIAL ≥ 1, never colliding with Odoo's `reva_issue_request_id` default of `0`.
- Dedup: a re-click while a run is still `pending` returns the **same** `request_id` (no second job), so the in-flight run's callback still matches.
- Behind the v1 router's API-key + rate-limit dependencies (inherited, not re-declared).

**Contract 2 — `POST {odoo}/api/reva/issues-created`** (`reva/odoo_client.py::issues_created`)
- Reuses `ODOO_CALLBACK_URL` — the client already derives the base URL from the write-field path, so `/issues-created` needs **no new configuration**. Same Bearer key (`ODOO_CALLBACK_API_KEY`).
- Success: `status:"created"` with the full issue list `[{number, title, url}]` — never `created` with an empty list (the reconcile path always carries ≥ 1; the create path is schema-bound to ≥ 1). `url` is GitHub's canonical `html_url` from the create response, not reconstructed from the user-typed repo URL.
- Failure: `status:"failed"`, `issues:[]`, short `error`. Sent best-effort on the final attempt of any failure; a callback failure never masks the original error.
- Response-code handling matches the handoff table: 4xx → `PermanentError`, do not retry (a 409 after successful creation — stale request_id / record not pending, the expected timeout-race outcome — is logged and the run stays `completed`); **5xx/network → retried with the contract's 30/120/300 s backoff**, implemented as `rq.Retry(max=3, interval=[30,120,300])` on the job (same policy the scheduler uses for reviews). The retried job resumes idempotently from the persisted plan, short-circuits when everything is created, and just re-sends the callback.

## The pipeline

| Piece | File |
|---|---|
| Route (accept/list/poll/requeue) | `api/app/routes/v1/ticket_issues.py` (+ `api/app/queries/ticket_issues.py`) |
| Request/response schemas | `api/app/schemas/ticket_issues.py` |
| TUI (Tickets tab: Issues column + per-ticket issue refs) | `tui/internal/ui/tickets.go`, `tui/internal/api/{types,client}.go` |
| URL validation | `reva/github_urls.py` |
| Claude planner (pure) | `reva/ticket_issue_planner.py` + `reva/ticket_issue_tool.py` + `prompts/ticket_issues.md` |
| Plan/result types | `reva/types.py` (`TicketIssueItem`, `TicketIssuePlan`, `TicketIssueJobParams`) |
| Worker runner (side effects) | `worker/worker/ticket_issue_runner.py` (`worker/worker/ticket_issue_tasks.py` is the stable RQ import shim) |
| GitHub search/create | `reva/github_client.py` (`find_issues_with_marker`, `create_issue`, `ensure_label`) |
| Odoo callback | `reva/odoo_client.py::issues_created` |
| Persistence | `db/migrations/012_ticket_issue_runs.sql`, `reva/db/models.py::TicketIssueRun`, `reva/db/writers.py` (ticket_issue_runs section) |
| PII retention | `writers.purge_old_ticket_issue_text`, wired into `scheduler/scheduler/main.py::maybe_purge_ticket_text` |

### Consultant DOCX (Contract 1 `description_docx`)

Project tasks may carry a consultant Word document (`{filename, content_base64}` or `null`). When present it is **the** planning basis — `description`/`analysis_html` are not sent to the planner. The worker extracts the text itself (`reva/docx_text.py`, stdlib zip + defusedxml — no docx dependency, both OOXML namespaces, content controls included) since the Messages API has no docx block. Hardened against hostile/oversized input: 422 at accept time for bad base64/non-zip (whitespace-wrapped base64 tolerated); inflated `document.xml` capped at 50 MB (zip-bomb, CWE-409); extracted text capped at 300k chars with an actionable "split the document" error; nginx's body cap raised to match the API's 26 MB (≈19 MB raw docx).

**The document is never stored server-side.** It rides the RQ job params (Redis) at first-plan time only; failed jobs carry a 7-day `failure_ttl`. The DB keeps only a 25-byte **`planning_basis`** digest (`"docx:<sha1>"` / `"text:<sha1>"`, migration 014) — enough to detect a revised spec without retaining the file. The retention purge still scrubs `description`/`analysis_html` and un-created plan-item bodies.

**Changed-document semantics:** plan-adoption and the marker are keyed on `planning_basis` (computed once at row creation, so it's stable across a run and its requeues). Re-click with unchanged inputs → re-link/resume as before. Re-click after the consultant revises the document → different basis → fresh plan and a fresh issue set (the old issues stay on GitHub for manual cleanup; re-linking them would point freelancers at a superseded spec). A requeue resumes the persisted plan; a docx run that *never* produced a plan can't be re-planned (the file is gone) and the requeue returns 409 ("re-trigger from Odoo").

### Claude planning

`TicketIssuePlanner` mirrors `TicketAnalyzer`: Messages API with a forced `submit_ticket_issues` tool call, schema derived from the Pydantic model (drift-proof), `PermanentError` on missing/invalid tool output. Specifics:

- Inputs are the ticket `name` + `description` and — when present — the completed `analysis_html`, whose acceptance criteria / test cases are the intended basis for the split (handoff §implementation). All three are customer-derived, so all sit inside per-call **nonce delimiters** with a data-not-instructions framing (SECU-5).
- `max_tokens=16384`: ten full issue bodies can exceed the 8192 default, and truncation surfaces as an unretried `PermanentError`.
- The prompt orders: split only into independently deliverable pieces, one issue when in doubt, ≤ 10, always in English, no invented requirements, and the `issues` array must be structured — never a JSON-encoded string (a stringified array with broken escaping caused a production validation failure; such failures are classified transient so RQ re-plans).
- Spend is recorded in the `claude_spend` ledger (`kind="ticket_issues"`), so the rolling budget cap counts it.

### Issue creation & dedup (reconcile, not skip)

Every issue body ends with a hidden marker `<!-- revaticket<sha1(lowercase owner/repo, model_name, ticket_id)[:16]> -->` (lower-cased because GitHub treats repo names case-insensitively while Odoo's URL is free text — an admin fixing the URL's casing must not change the key).

The marker is **ticket-level by design** — *not* the audit pattern's per-finding title hash. Issue titles are Claude-generated and drift between plans; a title-keyed marker would stop matching on every re-run and duplicate the whole set. Run resolution order:

1. **Resume** — this run's row already has a persisted plan (requeue / RQ retry): create only items without an issue number, no Claude call. When everything already has a number, the GitHub setup calls are skipped entirely and only the callback is re-sent.
2. **Adopt** — fresh run (re-click after the timeout race or a partial failure): the latest prior run for the same record **and same repo** with a persisted plan is adopted from REVA's own DB. This is authoritative where GitHub search is not — it includes the not-yet-created remainder of a partial failure and is immune to search-index lag, so a re-click *completes* a partial set instead of truncating it.
3. **Reconcile** — no plan anywhere in the DB (e.g. DB reset): marker search (open **and** closed issues) finds existing ones → complete with that list and re-link it via the callback.
4. **Plan** — Claude plan → **persist the plan before any GitHub call** → create each issue (progress persisted after every create; created items keep only `{title, number, url}` — the body lives on GitHub) → callback with the full set.

Issue **titles** are `[Task <ticket_id>] <n>/<total> — <planned title>` (`Task`/`Ticket` from the Odoo model): the record id makes every issue traceable from the GitHub list alone, and n/total marks the intended implementation order — the prompt instructs the planner to return issues in that order, and the numbering survives GitHub's sorting. The full title is what's stored and sent to Odoo, so all surfaces match GitHub. (GitHub *sub-issues* were considered for grouping instead and deliberately deferred — they'd touch parent lifecycle, dedup/resume, Contract 2, and state sync for polish the numbering already provides.)

Issue bodies carry the requirement text, an acceptance-criteria checklist, the **mandatory** `ticket_url` back-link, and the `reva-ticket` label (created per-repo, best-effort).

### Per-issue state sync (Contract 3)

Each issue item carries a `state` (`open` at creation, synced thereafter). When the GitHub App delivers an `issues` webhook (`closed`/`reopened`) for an issue labeled `reva-ticket`:

1. `webhooks.py` enqueues `worker.ticket_issue_tasks.sync_ticket_issue_state` (with the same retry policy as the callbacks); unlabeled issues never hit the DB.
2. The worker records the state on **every** run carrying that issue (adopted/reconciled runs share issues; the free-text `github_url` is matched case-insensitively via `parse_github_repo_url`).
3. Per affected Odoo record it POSTs `{odoo}/api/reva/issue-state` with the changed `number`/`state` plus the **full snapshot** — Odoo re-renders the links idempotently (closed issues struck through with ✓) and posts a chatter note. A 409 (record not in `created`) is permanent and swallowed — the DB state is recorded and the next `issues-created` callback carries it anyway; 5xx rides the RQ retries.

The TUI **Tickets tab** is the union of the analysis and create-issues feeds: one row per Odoo record that has an analysis, an issue run, or both (newest activity first) — so a task that only had issues created (never analyzed) still appears, with a blank Analysis cell and the Issues count. **Enter** on a ticket drills into its full issue list (number, open/`done ✓`/`not created` state, title; `o` opens the highlighted issue on GitHub, `esc` returns). Per-issue done state **requires the GitHub App to be subscribed to "Issues" events** (app settings on GitHub — without it the close/reopen webhook never arrives).

### Failure semantics

| Failure | Run row | Callback | Recovery |
|---|---|---|---|
| Transient error (Claude 529, GitHub 5xx, malformed plan output, …) with RQ retries remaining | stays `pending` | — | RQ retries with 30/120/300 s backoff; the rerun resumes from the persisted plan (or re-plans if validation failed) |
| Transient error on the final attempt | `failed` | `failed` | User re-clicks (adopts the plan if one was persisted) |
| GitHub App not installed on the repo (`get_repo_installation_id` 404) | `failed` | `failed` | Install the app, user re-clicks |
| Claude returned no tool call (permanent) | `failed` | `failed` | User re-clicks (re-plan) |
| Partial creation, then GitHub error | `failed`, created subset persisted in `issues` | `failed` | User re-click **adopts the plan and creates the missing items** (full set in the callback); ops requeue does the same on the original run |
| Callback 5xx/network after successful creation | stays `completed` | retried | RQ retry short-circuits (nothing to create) and re-sends the callback |
| Callback 409/4xx after successful creation | stays `completed` | — | Logged and swallowed (contract: do not retry); Odoo moved on — the re-click path re-links via adoption |
| Queue down at accept/requeue time | `failed` (`enqueue failed: …`) | — | Odoo gets 503, rolls back, button stays enabled; user retries |
| Worker SIGKILLed mid-job (row orphaned `pending`) | `pending` | — | Requeue accepts stale (> 30 min) pending runs |

One deliberate divergence from the ticket-analysis runner: **no `reset_status` call** — that endpoint belongs to the *analysis* state machine (`reva_status`); the issue flow's state machine (`reva_issue_status`) is driven entirely by Odoo's button and the `issues-created` callback.

### Ops endpoints

- `GET /api/v1/ticket-issue-runs` — paginated runs feed (newest first, `status` filter). Issue items are stripped to `{number, title, url}` — plan bodies (customer-derived text) never leave through the list. Feeds the TUI.
- `GET /api/v1/create-issues/{request_id}` — status/result. Omits `description`/`analysis_html` (PII), mirroring how ticket-analysis status omits `input_text`.
- `POST /api/v1/create-issues/{request_id}/requeue` — re-run a `failed`/`completed` run, or a **stale pending** one (> 30 min — its job died without running). Refuses (`409`) when the inputs were purged and no plan exists (it would plan from the purge sentinel), and when a different run is already pending for the same record (the unique pending-per-record index would reject it). Note the callback of a requeued run only lands while Odoo still waits on the same `request_id` — otherwise it gets a 409, which is logged and leaves the run `completed`.

### Data retention (F1/SECU-8)

`ticket_issue_runs.description` and `.analysis_html` are customer-authored text and are scrubbed by the scheduler's existing retention pass (same sentinel and cadence as `ticket_analyses.input_text`). The `issues` JSON (titles/numbers/urls — already public on GitHub) is derived data and kept, like `result_html`.

## Configuration

No new environment variables.

- `ODOO_CALLBACK_URL` / `ODOO_CALLBACK_API_KEY` — already used by the write-field callback; the issues-created path is derived from the same base URL.
- Deployment note: Odoo omits the `Authorization` header entirely when `reva.api_key` is unset on its side — a REVA deploy with `REVA_API_KEY` set will 401 such an Odoo. Set both or neither.
- Odoo side (Settings → REVA): `reva.url`, `reva.api_key`, `reva.callback_api_key`, plus the GitHub Project URL on each project (Settings tab).

## Known limitations

- GitHub's search API is eventually consistent. The marker-search path is only a fallback (the DB-first adoption handles the realistic re-click cases), but a fresh run against a wiped DB shortly after issues were created could still miss them.
- Bulk-creating up to 10 issues can trip GitHub's secondary rate limit (a 403 → `PermanentError` mid-loop). Progress is persisted per-issue, so the re-click/requeue path completes the set.
- The planner does not gate on the daily budget cap before calling Claude (mirrors ticket analysis); it does record its spend in the ledger, so the cap counts it for the gated paths (reviews/audits/replies).
- RQ retries fire on permanent errors too (rq 2.9 retries any exception); the reruns are cheap (resume/short-circuit) and duplicate `failed` callbacks land as 409s that the best-effort sender swallows — bounded noise, no incorrectness.

## Tests

```bash
worker/.venv/bin/pytest worker/tests -q       # planner, runner, writers, clients, URL parsing
api/.venv/bin/pytest api/tests -q             # contract tests (handoff payload verbatim), dedup, requeue guards
scheduler/.venv/bin/pytest scheduler/tests -q # retention purge
```

Key suites: `worker/tests/test_ticket_issue_planner.py`, `worker/tests/test_ticket_issue_runner.py`, `api/tests/test_v1_ticket_issues.py`, plus additions to `test_db.py`, `test_odoo_client.py`, `test_github_client.py`, `test_github_urls.py`, `test_retention.py`.

For an end-to-end check against a dev Odoo (no GitHub needed), the handoff doc ships a curl harness simulating the callback cycle (§"Testing against a dev Odoo").
