# Timesheet Wording Review — Design

Date: 2026-07-03
Status: approved (design Q&A with Joseph, 2026-07-03)

## Context

Odoo timesheet lines (time bookings on projects/tasks) end up customer-facing —
on invoices and reports. Descriptions written by developers/consultants/sales
often contain wording that shouldn't reach a customer. REVA gets a new job type:
Odoo sends a batch of booking lines, REVA rewrites the bad descriptions via the
Messages API, and posts the results back to an Odoo callback endpoint that
applies the changes and marks the lines as checked.

This rides the existing ticket-analysis rails: per-instance-key POST →
DB row → RQ job → Claude Messages API (structured tool output) →
`OdooCallbackClient` callback. No repo, no headless CLI, no scheduler
involvement.

## Decisions (from design Q&A)

1. **Flow**: async + callback, like ticket-analysis. Odoo POSTs the batch, gets
   202, worker processes, REVA sends **one** callback with all results.
2. **Output scope**: only changed lines come back. Statuses per line:
   `ok` (omitted from callback), `rewritten` (with `updated_desc`),
   `needs_human` (with a short `reason`). Odoo marks every line of the batch as
   checked except `needs_human` ones.
3. **Word list**: Odoo sends the flagged-word list in each request
   (`flagged_words`). REVA stores no word config.
4. **Language**: descriptions may be German or English; rewrites preserve the
   line's original language. `needs_human` reasons are written in the line's
   language too.
5. **Role rules**: `developer` lines may stay general ("Implementing",
   "Design" are acceptable); `consultant` and `sales` lines need a meaningful
   customer-facing description. If one can't be built from the given context
   (task name, project name, description), the line is flagged `needs_human` —
   Claude must not invent activities.
6. **Bad wording scope** (beyond the provided word list): unprofessional tone
   (slang, frustration), negative framing of the work ("tried to fix", "still
   broken", "wasted time"), spelling & grammar. Explicitly **not** in scope:
   internal jargon, ticket numbers, or people's names — leave those alone
   unless they appear in `flagged_words`.
7. **Batching**: cadence/volume from Odoo is undecided and must not matter.
   The worker chunks each request into 100-line chunks (code constant), one
   Messages API call per chunk, processed **sequentially in one RQ job**.
8. **Persistence**: metadata only — run row + per-line statuses keyed by
   `line_id`. Original and updated description texts are never stored at rest,
   with one approved exception: the assembled callback payload (contains
   `updated_desc` texts) is kept on the run row **only until the callback
   succeeds**, then cleared. This is what makes a callback-only failure
   retryable without re-paying Claude (same idempotency shape as
   ticket-analysis's persisted `result_html`).

## External API contract

### Request: `POST /api/v1/timesheet-review`

Instance-key gated (`require_odoo_instance`, same as `/ticket-analysis`).
Registered on the `_instance` router.

```json
{
  "request_id": "odoo-generated string, unique per batch",
  "flagged_words": ["optional", "words"],
  "lines": [
    {
      "line_id": 123,
      "task_name": "…",
      "project_name": "…",
      "user_name": "…",
      "user_role": "developer",
      "description": "…"
    }
  ]
}
```

Validation (422 on violation):

- `user_role` ∈ {`developer`, `consultant`, `sales`} (strict; both sides are
  ours, changes are coordinated).
- `lines` non-empty, ≤ 5000 items; `description` ≤ 4000 chars each.
- `flagged_words` ≤ 500 items, each ≤ 100 chars.
- `request_id` non-empty, ≤ 128 chars.

Response `202`: `{"run_id": …, "job_id": "…", "status": "pending"}`.

Semantics, mirroring ticket-analysis:

- **Dedup**: a pending run for the same `(odoo_instance_id, request_id)`
  returns the existing run (idempotent re-POST, no double pay). Enforced by a
  partial unique index (`WHERE status = 'pending'`); the IntegrityError race
  fallback returns the winner, as in `submit_ticket_analysis`.
- **Stale pending**: a pending run older than 60 min has no live job (job
  timeout + retry backoff are well under that). The POST handler marks it
  failed and creates a fresh run, so a SIGKILLed worker can't wedge the
  `request_id` forever. (No requeue endpoint needed — unlike tickets, Odoo is
  not stuck waiting: unchecked lines are simply resent in the next batch.)
- **Enqueue failure** → row marked failed + 503, so the dedup can't pin future
  submits to a row no worker will process.
- Completed/failed runs do **not** block a re-POST of the same `request_id`.

RQ enqueue parameters: `job_timeout = max(600, 120 * n_chunks)` seconds,
`Retry(max=3, interval=[60, 300, 900])`, `failure_ttl = 7 days` (job args in
Redis carry the description texts, same situation as ticket text today — the
TTL caps that retention).

### Callback: `POST {odoo_base}/hr/timesheet-results`

(Odoo-side API namespacing, 2026-07-05: each Odoo app namespaces its REVA
endpoints — tickets under `/tickets/`, the timesheet app under `/hr/`.)

Sent via a new `OdooCallbackClient.timesheet_results()` (same bearer auth,
`_post()` error mapping: 4xx permanent, 5xx/network transient).

```json
{
  "request_id": "…",
  "results": [
    {"line_id": 123, "status": "rewritten", "updated_desc": "…"},
    {"line_id": 456, "status": "needs_human", "reason": "…"}
  ],
  "stats": {"total": 250, "ok": 220, "rewritten": 25, "needs_human": 5}
}
```

`ok` lines are omitted from `results`. Response 200/204 = success. Failure
handling: run failures never reach Odoo — the affected lines stay unchecked
there and arrive again in the next batch (self-healing; no Odoo-side error UI).

## Worker

New files mirroring the ticket split: `worker/worker/timesheet_tasks.py` (RQ
entry `run_timesheet_review(job_params: dict)`) and
`worker/worker/timesheet_runner.py` (logic). Reuses `get_context()` and
`build_odoo_client()`.

Run loop:

1. Load the run row; if `status == 'completed'` and `callback_payload` is
   already cleared (`callback_sent_at` set), return — RQ retry after full
   success (idempotency guard).
2. If `status == 'completed'` and `callback_payload` present → skip to step 6
   (callback-only retry, no Claude calls).
3. Chunk `params.lines` into `_CHUNK_SIZE = 100`. For each chunk:
   - **Resume check**: `line_id`s already present in `timesheet_review_lines`
     for this run → skip the chunk (a retried job re-pays only unfinished
     chunks).
   - **Budget check** before the call:
     `sum_estimated_cost_since(db, now-24h, serialize=True)` vs
     `REVA_DAILY_BUDGET_USD`; on exceed raise `TransientError` (RQ backs off).
   - Call `TimesheetAnalyzer.analyze_chunk()` (Messages API,
     `REVA_DEFAULT_MODEL`).
   - **Coverage validation**: the tool result must contain exactly the chunk's
     `line_id`s. On mismatch retry the chunk **once**; line_ids still missing
     become `needs_human` with reason "no result returned" (never silently
     dropped). Extra/unknown line_ids are discarded with a warning log.
   - Persist atomically (one transaction): insert `timesheet_review_lines`
     rows (status + reason only), merge the chunk's rewritten/needs_human
     entries into `run.callback_payload` (JSON), accumulate token counts.
   - Record spend: `record_claude_spend(db, "timesheet_review", cost)` per
     chunk.
4. Finalize: compute stats from the line rows, mark run `completed`.
5. (Failure path: `PermanentError` or unexpected exception → mark run failed
   with error text; `TransientError` propagates for RQ retry — same shape as
   `run_ticket_analysis`.)
6. Send `odoo.timesheet_results(request_id, results, stats)` from
   `callback_payload`. On success set `callback_sent_at` and **clear
   `callback_payload`** (texts leave REVA). On failure the exception propagates
   (RQ retries; step 2 resends without re-analyzing).

## Claude integration (`reva/`)

- **`reva/timesheet_analyzer.py`** — `TimesheetAnalyzer(claude, prompts_dir)`
  modeled on `TicketAnalyzer`. `analyze_chunk(params, chunk) ->
  (response, list[TimesheetLineResult])`.
  - System blocks (via `prompt_builder` conventions, cache_control on the
    static block): instructions from `prompts/timesheet_review.md`, plus a
    per-request block with the `flagged_words` list. Both are identical across
    a run's chunks → prompt-cached across the sequential calls.
  - User prompt: the chunk's lines, each field (task/project/user names,
    description) **nonce-fenced** as untrusted content, following the existing
    prompt-injection guard convention. Role and line_id are trusted metadata
    outside the fence.
  - Forces the `submit_timesheet_review` tool (`tool_choice`), validates the
    tool input, maps API errors to Transient/Permanent like the ticket path.
- **`reva/timesheet_tool.py`** — tool schema `submit_timesheet_review`:
  array of `{line_id: int, status: "ok"|"rewritten"|"needs_human",
  updated_desc?: string, reason?: string}`. Validation: `updated_desc` required
  iff `rewritten` (non-empty; if byte-identical to the original the line is
  downgraded to `ok`), `reason` required iff `needs_human`.
- **`prompts/timesheet_review.md`** — the rules from Decisions 4–6: minimal
  edits, preserve meaning and language, never invent activities; fix tone /
  negative framing / spelling+grammar / flagged words; leave internal jargon
  and names alone; role expectations (developer general OK, consultant & sales
  need customer-meaningful text or `needs_human`); `ok` means acceptable as-is
  — no style churn; reasons in the line's language.
- **`reva/types.py`** — `TimesheetLine`, `TimesheetJobParams(run_id,
  odoo_instance_id, request_id, flagged_words, lines)`,
  `TimesheetLineResult(line_id, status, updated_desc, reason)`.
- **`reva/odoo_client.py`** — `timesheet_results()` method as specced above.

## DB (`db/migrations/025_timesheet_reviews.sql` + ORM models)

Idempotent SQL, `BIGSERIAL` primary keys, matching models in
`reva/db/models.py` (tests build from the models), writers in
`reva/db/writers.py` following the ticket-analysis naming
(`record_timesheet_run_created`, `get_pending_timesheet_run`,
`record_timesheet_run_completed/failed`, `attach_timesheet_job_id`,
`get_timesheet_run`, `record_timesheet_lines`, `clear_timesheet_callback_payload`, …).

`timesheet_review_runs`:

| column | notes |
|---|---|
| `id` | BIGSERIAL PK (= `run_id`) |
| `odoo_instance_id` | FK odoo_instances |
| `request_id` | text |
| `status` | pending / completed / failed |
| `total_lines`, `ok_count`, `rewritten_count`, `needs_human_count` | ints |
| `model`, `input_tokens`, `output_tokens`, `estimated_cost_usd` | cost tracking |
| `callback_payload` | JSON, nullable — cleared after successful callback |
| `callback_sent_at` | timestamptz, nullable |
| `job_id`, `error`, `created_at`, `completed_at` | as in ticket_analyses |

Partial unique index: `(odoo_instance_id, request_id) WHERE status = 'pending'`.
Index on `created_at`. (Partial-index behavior on real Postgres is covered by
`make test-integration` / staging, per the standing migration caveat.)

`timesheet_review_lines`: `id`, `run_id` (FK, cascade), `line_id`, `status`,
`reason` (nullable — Claude-generated, no source text), `created_at`; unique
`(run_id, line_id)`. **No description columns.**

## Internal read API + TUI

Per the "Keep the TUI in Sync" rule:

- `GET /api/v1/timesheet-reviews` (master-key router): paginated run list —
  id, instance, request_id, status, counts, cost, created/completed. Schema in
  `api/app/schemas/`, query in `api/app/queries/`, mirroring
  `list_ticket_analyses`.
- TUI: new `timesheets.go` tab modeled on `tickets.go` — runs table with
  status, ok/rewritten/needs_human counts, cost. Client/iface/mock/types
  extended in `tui/internal/api/`. Demo mode gets fixture rows.
  `go build/vet/test ./...` stays green.

## Error handling summary

| failure | behavior |
|---|---|
| invalid payload / bad instance key | 422 / 401 at accept time (Odoo sees it) |
| queue down | 503, row failed |
| Claude transient (429/5xx/timeout) | `TransientError` → RQ retry, resume skips done chunks |
| budget cap hit | `TransientError` before the call → RQ backoff |
| Claude permanent / validation exhausted | run failed, error persisted; lines return in Odoo's next batch |
| coverage mismatch | one chunk retry, then `needs_human` per missing line |
| callback 4xx | `PermanentError` → run failed (payload kept for inspection) |
| callback 5xx/network | `TransientError` → RQ retry resends stored payload, no re-pay |
| worker SIGKILL mid-run | stale-pending takeover on next POST of the same request_id |

No Google Chat notification (matches ticket-analysis; failures are visible in
the TUI and logs, and the flow self-heals via Odoo's next batch).

## Testing

- **api**: auth, each 422 rule, pending dedup + race fallback, stale-pending
  takeover, enqueue-failure 503, list endpoint. SQLite in-memory.
- **worker**: chunking edges (1 / 100 / 101 / 250 lines), resume-skip on
  retry, coverage-mismatch retry + `needs_human` fallback, identical-rewrite
  downgrade to `ok`, callback payload assembly + clearing, callback-only retry
  performs zero Claude calls, budget-exceeded raises before calling, spend
  recorded per chunk, stats correctness. Claude mocked at the client boundary.
- **reva**: tool schema validation matrix, prompt assembly (fencing, role
  rules, flagged words present, cache_control placement),
  `timesheet_results()` error mapping via httpx MockTransport.
- **tui**: `go build ./... && go vet ./... && go test ./...` with mock client
  coverage for the new tab.
- `ruff` on all touched Python; `make test` (shared `reva/` changes touch all
  three services).

## Out of scope

- Storing description texts for later auditing (explicitly declined).
- Odoo-side implementation (endpoint that applies changes + marks lines
  checked) — contract above is the interface.
- Word-list management UI in REVA (Odoo owns the list).
- Fan-out/parallel chunk processing (sequential is enough at these volumes).
