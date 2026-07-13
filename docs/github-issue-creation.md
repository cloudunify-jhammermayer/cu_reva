# GitHub Issue Creation from Odoo Tickets

REVA's side of the **github-issues handoff** (`ast-odoo/custom_addons/cu_reva_ticket_analysis/docs/github-issues-handoff.md`, module 19.0.2.0.0). An Odoo user clicks **Create Issues** on a Helpdesk ticket / Project task; REVA plans 1–10 GitHub issues with Claude, creates them in the project's repository, and reports back so Odoo renders the links. Freelancers without Odoo access work from those issues.

Implemented 2026-06-10. The Odoo side was already shipped and tested — REVA conforms to it, not the other way around.

> Optionally, a request can also carry a **GitHub Project board URL** + **plan date**; REVA then adds the created issues (and the epic) to that Projects v2 board and echoes per-issue planned/completed dates back to Odoo. See [GitHub Projects board for ticket issues](github-projects.md).

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
- Request: `{ticket_id, model_name, github_url, name, description, analysis_html, priority, ticket_url}` (`api/app/schemas/ticket_issues.py`) plus an **optional** `issue_type` — one of `BUG FEAT CR CONF DEV MIG SUP DOC` (an empty string is treated as unset; `422` on any other value). When set (the Odoo wizard flow) every issue of the request gets that fixed type; omitted (the analysis flow) the planner picks a type per issue. Do **not** add required fields — the Odoo payload is fixed; optional additive fields are fine.
- `github_url` is validated here (`reva/github_urls.py`; tolerates trailing `/` and `.git`) → `422` on anything that isn't `https://github.com/{owner}/{repo}`. Odoo shows the error and rolls its record back — fail fast beats an async failure.
- `github_url` is also **reachability-checked** at accept time: `GitHubClient.get_repo_installation_id(owner, repo)` (the App must be installed on the repo to create issues there). A definitive miss (GitHub 404/403 → `PermanentError`) → `422`; a GitHub blip (`TransientError`) is accepted, since the worker re-checks before creating and we won't turn an outage into a user-facing rejection.
- Response: `202 {"request_id": <ticket_issue_runs.id>, "job_id", "status"}`. Odoo stores `request_id` and the callback must echo it. The row id is a BIGSERIAL ≥ 1, never colliding with Odoo's `reva_issue_request_id` default of `0`.
- Dedup: a re-click while a run is still `pending` returns the **same** `request_id` (no second job), so the in-flight run's callback still matches.
- Behind the v1 router's API-key + rate-limit dependencies (inherited, not re-declared).

**Contract 2 — `POST {odoo}/api/reva/issues-created`** (`reva/odoo_client.py::issues_created`)
- Reuses `ODOO_CALLBACK_URL` — the client already derives the base URL from the write-field path, so `/issues-created` needs **no new configuration**. Same Bearer key (`ODOO_CALLBACK_API_KEY`).
- Success: `status:"created"` with issue items `[{number, title, url, state, plan_date, complete_date, estimate_hours}]`. The list is the **union of issues across all of the record's runs** (deduped by number, newest run wins title/url/state) — Odoo's handler replaces the record's whole list, so sending only the completing run's issues would wipe what earlier requests created (multiple requests per ticket accumulate). Never `created` with an empty list (the reconcile path always carries ≥ 1; the create path is schema-bound to ≥ 1). `url` is GitHub's canonical `html_url` from the create response, not reconstructed from the user-typed repo URL. The `issues-created` callback also carries a top-level `total_estimate_hours` — the sum over union items that have an estimate, `null` when none do.
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

### Consultant file (Contract 1 `description_docx`: .docx / .pdf / .txt)

Project tasks may carry a consultant file (`{filename, content_base64}` or `null`) — `.docx`, `.pdf`, or `.txt`. When present it is **the** planning basis — `description`/`analysis_html` are not sent to the planner. Type dispatch lives in `reva/attachment_text.py`: the **filename extension is the authoritative gate** (`.xlsx`/`.pptx` share the zip magic with `.docx`, so content-sniffing alone can't tell them apart), then the bytes are verified against it. `classify_attachment` is the cheap accept-time gate → `422` for an unsupported extension, bad base64, or content/extension mismatch (whitespace-wrapped base64 tolerated); `extract_attachment_text` is the worker-side extractor: `.docx` via `reva/docx_text.py` (stdlib zip + defusedxml, both OOXML namespaces, content controls), `.pdf` via `pypdf` (worker-only dep, lazy-imported, text only — no rendering/JS), `.txt` decoded as UTF-8. Extracted text is capped at 300k chars; the 26 MB API body cap (CWE-409, plus the docx `document.xml` 50 MB inflate cap) bounds the input. A file that passes the accept-time sniff but yields no text / can't be parsed fails the worker with a `PermanentError` → failed callback to Odoo.

**The file is never stored server-side.** It rides the RQ job params (Redis) at first-plan time only; failed jobs carry a 7-day `failure_ttl`. The DB keeps only a 25-byte **`planning_basis`** digest (migration 014); the `"docx:<sha1>"` prefix is kept for **any** attachment type (`.docx`/`.pdf`/`.txt`) so the dedup digest and GitHub marker stay stable, vs `"text:<sha1>"` for description+analysis — enough to detect a revised spec without retaining the file. The retention purge still scrubs `description`/`analysis_html` and un-created plan-item bodies.

**Changed-document semantics:** plan-adoption and the marker are keyed on `planning_basis` (computed once at row creation, so it's stable across a run and its requeues). Re-click with unchanged inputs → re-link/resume as before. Re-click after the consultant revises the document → different basis → fresh plan and a fresh issue set (the old issues stay on GitHub for manual cleanup; re-linking them would point freelancers at a superseded spec). A requeue resumes the persisted plan; an attachment run that *never* produced a plan can't be re-planned (the file is gone) and the requeue returns 409 ("re-trigger from Odoo").

### Claude planning

`TicketIssuePlanner` mirrors `TicketAnalyzer`: Messages API with a forced `submit_ticket_issues` tool call, schema derived from the Pydantic model (drift-proof), `PermanentError` on missing/invalid tool output. Specifics:

- Inputs are the ticket `name` + `description` and — when present — the completed `analysis_html`, whose acceptance criteria / test cases are the intended basis for the split (handoff §implementation). All three are customer-derived, so all sit inside per-call **nonce delimiters** with a data-not-instructions framing (SECU-5).
- `max_tokens=16384`: ten full issue bodies can exceed the 8192 default, and truncation surfaces as an unretried `PermanentError`.
- The prompt orders: split only into independently deliverable pieces, one issue when in doubt, ≤ 10, always in English, no invented requirements, and the `issues` array must be structured — never a JSON-encoded string (a stringified array with broken escaping caused a production validation failure; such failures are classified transient so RQ re-plans).
- Spend is recorded in the `claude_spend` ledger (`kind="ticket_issues"`), so the rolling budget cap counts it.

### Issue creation & dedup (reconcile, not skip)

Every issue body ends with a hidden marker `<!-- revaticket<sha1(lowercase owner/repo, model_name, ticket_id, planning_basis)[:16]> -->` (lower-cased because GitHub treats repo names case-insensitively while Odoo's URL is free text — an admin fixing the URL's casing must not change the key). The `planning_basis` is part of the digest so a revised spec (changed description/docx) gets a fresh issue set instead of reconciling against the old one; a **typed** request prefixes its basis with the lowercased type code (`cr:text:…`), so the same text sent typed vs untyped plans separately. Synthesized **parent** ("epic") issues carry an additional `<!-- revaticketparent<same digest> -->` marker so reconciliation (after a DB wipe) can tell the parent apart from its children.

The marker is **ticket-level by design** — *not* the audit pattern's per-finding title hash. Issue titles are Claude-generated and drift between plans; a title-keyed marker would stop matching on every re-run and duplicate the whole set. Run resolution order:

1. **Resume** — this run's row already has a persisted plan (requeue / RQ retry): create only items without an issue number, no Claude call. When everything already has a number, the GitHub setup calls are skipped entirely and only the callback is re-sent.
2. **Adopt** — fresh run (re-click after the timeout race or a partial failure): the latest prior run for the same record **and same repo** with a persisted plan is adopted from REVA's own DB. This is authoritative where GitHub search is not — it includes the not-yet-created remainder of a partial failure and is immune to search-index lag, so a re-click *completes* a partial set instead of truncating it.
3. **Reconcile** — no plan anywhere in the DB (e.g. DB reset): marker search (open **and** closed issues) finds existing ones → complete with that list and re-link it via the callback.
4. **Plan** — Claude plan → **persist the plan before any GitHub call** → create each issue (progress persisted after every create; created items keep only `{title, number, url}` — the body lives on GitHub) → callback with the full set.

Issue **titles** are `[<TYPE>] <ticket_id> - <tldr>` with the sequence suffix ` (<n>/<total>)` appended only when the request yields ≥ 2 issues (a single-issue request has no suffix). The planner returns a ≤ 30-char tldr; the runner hard-truncates it as a backstop. The type code (`BUG`/`FEAT`/`CR`/`CONF`/`DEV`/`MIG`/`SUP`/`DOC`) is the request's fixed type when set, else the planner's per-issue pick, falling back to `DEV` for plans persisted before the type rollout. The record id makes every issue traceable from the GitHub list alone, and n/total marks the intended implementation order. The full title is what's stored and sent to Odoo, so all surfaces match GitHub.

**One epic per ticket+repo.** When a request yields ≥ 2 issues and no parent exists yet, the runner creates a parent ("epic") issue titled `[<dominant-type>] <ticket_id> - <ticket-name tldr>` (dominant = the most common child type, tie → the first child's; no n/total suffix) and attaches the children as GitHub sub-issues. A later request for the same ticket **adopts** that existing parent (from REVA's DB) and attaches its issues to it — even a single new issue — instead of spawning a second epic.

Issue bodies carry the requirement text, an acceptance-criteria checklist, the **mandatory** `ticket_url` back-link, and two labels: `reva-ticket` and the issue's type code (both created per-repo, best-effort).

### Per-issue state sync (Contract 3)

Each issue item carries a `state` (`open` at creation, synced thereafter). When the GitHub App delivers an `issues` webhook (`closed`/`reopened`) for an issue labeled `reva-ticket`:

1. `webhooks.py` enqueues `worker.ticket_issue_tasks.sync_ticket_issue_state` (with the same retry policy as the callbacks); unlabeled issues never hit the DB.
2. The worker records the state on **every** run carrying that issue (adopted/reconciled runs share issues; the free-text `github_url` is matched case-insensitively via `parse_github_repo_url`).
3. Per affected Odoo record it POSTs `{odoo}/api/reva/issue-state` with the changed `number`/`state` plus the **full snapshot** (the same union across all of the record's runs as the issues-created callback) — Odoo re-renders the links idempotently (closed issues struck through with ✓) and posts a chatter note. A 409 (record not in `created`) is permanent and swallowed — the DB state is recorded and the next `issues-created` callback carries it anyway; 5xx rides the RQ retries.

The TUI **Tickets tab** is the union of the analysis and create-issues feeds: one row per Odoo record that has an analysis, an issue run, or both (newest activity first) — so a task that only had issues created (never analyzed) still appears, with a blank Analysis cell and the Issues count. **Enter** on a ticket drills into its full issue list (number, open/`done ✓`/`not created` state, title; `o` opens the highlighted issue on GitHub, `esc` returns). Per-issue done state **requires the GitHub App to be subscribed to "Issues" events** (app settings on GitHub — without it the close/reopen webhook never arrives).

### Estimate sync (Odoo → board)

Estimates live in exactly two places: the run rows' `estimate_hours` (echoed to Odoo on every callback) and the project board's **Estimate** NUMBER field. Issue **bodies never render them** — a body estimate would go stale the moment someone edits it in Odoo.

When a user edits an issue's estimate in Odoo's issue table, the addon POSTs `/api/v1/update-issue-estimate` (`{ticket_id, model_name, number, estimate_hours}`, instance-key gated, no budget check — the job makes GitHub API calls only). The route rejects an unknown issue with 404 (Odoo blocks the edit and rolls back — no silent drift), otherwise enqueues `worker.ticket_issue_tasks.update_issue_estimate`:

1. Writes the new `estimate_hours` on the issue across **every** run carrying it (the union feeds later callbacks — a single-row update would resurrect the old value).
2. Mirrors the value to the board's Estimate field via the persisted `project_item_id` (paired with the `github_project_url` of the run that placed it). No board / never placed → the DB update is all there is (`no_board`). Estimate field deleted since placement → ops event `estimate_field_missing`, job still succeeds.

No callback echoes the change back to Odoo — Odoo already holds the value, and the callback writes it does receive carry a context guard so they never bounce it back to REVA.

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

- `GET /api/v1/ticket-issue-runs` — paginated runs feed (newest first, `status` filter). Issue items are stripped to `{number, title, url, state, estimate_hours}` — plan bodies (customer-derived text) never leave through the list. Feeds the TUI.
- `GET /api/v1/create-issues/{request_id}` — status/result. Omits `description`/`analysis_html` (PII), mirroring how ticket-analysis status omits `input_text`.
- `POST /api/v1/create-issues/{request_id}/requeue` — re-run a `failed`/`completed` run, or a **stale pending** one (> 30 min — its job died without running). Refuses (`409`) when the inputs were purged and no plan exists (it would plan from the purge sentinel), and when a different run is already pending for the same record (the unique pending-per-record index would reject it). Note the callback of a requeued run only lands while Odoo still waits on the same `request_id` — otherwise it gets a 409, which is logged and leaves the run `completed`.

### Data retention (F1/SECU-8)

`ticket_issue_runs.description` and `.analysis_html` are customer-authored text and are scrubbed by the scheduler's existing retention pass (same sentinel and cadence as `ticket_analyses.input_text`). The `issues` JSON (titles/numbers/urls — already public on GitHub) is derived data and kept, like `result_html`.

## Configuration

No new environment variables.

- `ODOO_CALLBACK_URL` / `ODOO_CALLBACK_API_KEY` — already used by the write-field callback; the issues-created path is derived from the same base URL.
- Deployment note: Odoo omits the `Authorization` header entirely when `reva.api_key` is unset on its side — a REVA deploy with `REVA_API_KEY` set will 401 such an Odoo. Set both or neither.
- Odoo side (Settings → REVA): `reva.url`, `reva.api_key`, `reva.callback_api_key`, plus the GitHub Project URL on each project (Settings tab).
- Optional GitHub Projects (v2) board integration requires the App's org-level **Projects: Read & write** permission — see [github-projects.md](github-projects.md).

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
