# Test Guide — Features shipped 2026-07-01 → 2026-07-06

What landed in the last 5 days, what each feature does, and what to actually
test. Companion docs: `docs/session-feature-tldr-2026-07-05.md` (feature TLDR),
`docs/handoff-2026-07-06-prod-validation.md` (prod validation checklist —
tests 1–10 there are still the baseline smoke suite and are not repeated here).

**How to read this:** every feature below is unit-tested and green. Manual
testing effort should go to what unit tests *cannot* cover: live Claude calls,
real-Postgres migration SQL (031/032 and friends run raw SQL that SQLite tests
never execute), GitHub App permissions, Odoo callbacks, and the TUI against a
live server.

## Blocked until ast-odoo catches up

These REVA features are shipped and dormant — they no-op cleanly until the
Odoo side implements the receivers. Don't try to end-to-end test them yet:

| REVA feature | Missing Odoo piece |
|---|---|
| Ready signal | `POST {base}/tickets/ready` receiver |
| Merge change notes | `POST {base}/tickets/change-note` receiver |
| Issue assignee | Odoo sending `github_username` on create-issues |
| Timesheet review callback | `POST {base}/hr/timesheet-results` receiver (verify whether present) |

Sync flow: `scripts/sync_contracts.sh <ast-odoo-path>`, then implement against
`contracts/manifest.json` (`contracts_version` pin). Note: the TLDR references
`docs/ast-odoo-ticket-loop-and-value-report-guide.md`, which **does not exist**
in the repo — the contracts dir + specs are the actual source.

---

## 1. Ticket↔PR Loop Closure (`2c0f9e5` + fixes)

**What it is:** closes the loop Odoo ticket → GitHub issues → PR → back to
Odoo. Five parts, all optional-by-construction (no REVA-issue links → no-op):

1. **Structured analysis persistence** — ticket analyses store
   `result_structured` (summary + given/when/then ACs) in `ticket_analyses`.
2. **AC-grounded reviews** — a PR whose `Closes #N` refs resolve to
   REVA-created issues gets the ticket's acceptance criteria injected as a
   nonce-fenced `ticket_acceptance_criteria` skill param. Kill switch:
   `.claude-review.yml ticket_grounding: false`.
3. **Issue assignee** — `POST /api/v1/create-issues` accepts optional
   `github_username`; created issues (children + epic) get assigned; a GitHub
   422 degrades to unassigned + ops event `github/assignee_rejected`.
4. **Ready signal** — when the last linked GitHub issue closes, REVA POSTs the
   full issue union to `{base}/tickets/ready` (informational only — REVA never
   completes tickets).
5. **Merge change notes** — merged PR with closing refs → Messages-API call
   builds a consultant-language HTML note (`prompts/change_note.md`) → POST to
   `{base}/tickets/change-note`. Deduped per (repo, PR, ticket) in
   `change_notes` table. Kill switch: `change_notes: false`. Budget-gated
   (`skipped_budget` status).

**DB:** migration `031_ticket_pr_loop.sql` — `ticket_analyses.result_structured`,
`ticket_issue_runs.github_username`, new `change_notes` table.

**Surfaces:** dashboard metric `tickets_ready` (TUI dashboard "Ready" line),
`✔` marker on all-closed rows in the TUI Tickets tab, "Ready for deployment"
section in the weekly report.

### What to test

Testable **now** (no Odoo dependency):

- [ ] **Structured persistence**: run a ticket analysis, then
  `SELECT id, result_structured IS NOT NULL FROM ticket_analyses ORDER BY id DESC LIMIT 3;`
  — newest row must have structured JSON (summary + acceptance_criteria).
- [ ] **AC-grounded review (the flagship path)**: create issues from a ticket,
  open a PR with `Closes #<issue-number>` in the body, let the review run.
  Worker log must show `ticket_acs_attached`; the posted review should reason
  against the ACs. A PR with no closing refs, or refs to non-REVA issues,
  must review normally (no param, no errors).
- [ ] **Ready detection**: close all GitHub issues of one ticket → TUI Tickets
  tab shows the `✔` marker; dashboard shows `Ready N tickets`. The
  `tickets_ready` POST itself will land as an ops event
  (`odoo_callback/tickets_ready_rejected`) until Odoo implements the receiver
  — that warning appearing is *correct* behavior right now.
- [ ] **Change-note job enqueue**: merge a PR with `Closes #N` on a REVA
  issue → worker log `change_note_enqueued`, then a `change_notes` row:
  `SELECT status, length(note_html), estimated_cost_usd FROM change_notes;`
  Expect `completed` + non-empty HTML (the Odoo POST will fail visibly —
  ops event `odoo_callback/change_note_rejected` — until the receiver exists).
  Merging the same PR again (webhook redelivery) must not create a second row.
- [ ] **Migration on real Postgres**: `make test-integration` or verify staging
  boot applied 031/032 (`\d change_notes`, `\d value_reports`).

After ast-odoo ships: full cycle ticket → issues (assigned) → PR → merge →
change note visible as internal note in Odoo → all issues closed → consultant
sees ready notification. That cycle is the staging gate from the plan.

**Automated coverage:** `worker/tests/test_ticket_links.py`,
`test_reviewer.py` (grounding matrix), `test_ticket_issue_runner.py` (ready
signal), `test_change_note*.py` equivalents, `api/tests/test_webhooks.py`,
`tui` suite.

---

## 2. Monthly Value Reports (`2c0f9e5`, `d823ea8`)

**What it is:** scheduler fires monthly (`REVA_VALUE_REPORT_DAY=1`,
`REVA_VALUE_REPORT_HOUR_UTC=7`) → worker builds a pure-DB markdown rollup for
the previous calendar month (reviews, findings, resolved-by-fix rate, spend by
kind, per-repo / per-instance tables) → persisted in `value_reports` (one row
per period, re-runs replace). **No Claude call.** Google Chat delivery is
default-OFF (`REVA_VALUE_REPORT_CHAT_ENABLED=false`; `d823ea8` wired the var
into compose — before that fix, flipping it did nothing).

**API:** `GET /api/v1/value-reports`, `GET /api/v1/value-reports/latest`
(master key). No TUI surface (deliberate).

### What to test

- [ ] **Don't wait for Aug 1** — enqueue one manually:
  ```bash
  docker compose exec worker python -c "
  from worker.value_report_tasks import run_value_report
  run_value_report({'period_start_iso': '2026-06-01T00:00:00+00:00',
                    'period_end_iso': '2026-07-01T00:00:00+00:00'})"
  ```
  (or via an RQ enqueue). Then
  `curl -H "Authorization: Bearer $REVA_API_KEY" .../api/v1/value-reports/latest`
  — expect June's markdown with real numbers. Sanity-check the spend figures
  against the dashboard.
- [ ] **Re-run replaces**: run it again for the same period → still one row,
  `chat_sent` reset to false.
- [ ] **Chat stays silent**: with the flag unset, confirm no Chat message and
  `chat_sent=false`. Only flip `REVA_VALUE_REPORT_CHAT_ENABLED=true` after
  you've reviewed a few persisted reports (that was the explicit plan
  decision).
- [ ] **First scheduled fire**: on Aug 1 ~07:00 UTC check scheduler logs +
  `/value-reports/latest` covering July.

**Automated coverage:** `worker/tests/test_value_report{,_runner}.py`,
`scheduler/tests/test_value_report_due.py`, `api/tests/test_v1_value_reports.py`.

---

## 3. Triage Escalation Prepass (`211b5f2`, `c0b7e5a`)

**What it is:** a cheap Haiku call before push-triggered diff reviews that may
*upgrade* the mode (diff → full, or → deep for security-critical shapes:
ACLs/record rules, `sudo()`, raw SQL, migration scripts, auth exposure,
secrets). Escalate-only — never skips or downgrades. Human-triggered reviews
(`/review` etc.) bypass it. Decision stamped to `review_runs.triage_escalation`;
Haiku spend ledgered as `claude_spend.kind='triage'`; failures fail-open with
ops event `triage/decide_failed`.

**Flags:** `REVA_TRIAGE_ENABLED` default **false** (global), per-repo
`triage: false`. Both must be on.

### What to test

- [ ] Flip `REVA_TRIAGE_ENABLED=true` on staging/prod, restart worker.
- [ ] Push a PR touching `ir.model.access.csv` or adding `sudo()` → worker log
  `review_triage_escalated to=deep`;
  `SELECT review_mode, triage_escalation FROM review_runs ORDER BY id DESC LIMIT 3;`
  Check the resulting review actually ran the deeper skill/model (deep model in
  the run row / cost).
- [ ] Push a trivial one-liner → `triage_escalation IS NULL`, normal diff review.
- [ ] Cost sanity: `SELECT count(*), sum(estimated_cost_usd) FROM claude_spend WHERE kind='triage';`
  — should be cents, not dollars.
- [ ] Watch the escalation *rate* for a week: if most PRs escalate, the prompt
  is too eager — that's tuning data, not a bug.

**Automated coverage:** `worker/tests/test_triage.py`,
`test_reviewer_triage.py`, `test_triage_stamp.py`.

---

## 4. Scanner Feed — GitHub security alerts as review context (`a2a4cc3`, `b9675c0`)

**What it is:** open code-scanning / Dependabot / secret-scanning alerts are
collected per review and injected as a fenced `scanner_alerts` hint param
(hints, not findings to copy). Relevance-filtered: code-scanning only for
changed files, Dependabot only when a dependency manifest changed, secret
alerts always (with real `file:line` since `b9675c0` fetched locations).
Deterministic floor: any finding on a file with an open secret alert is raised
to `critical`. **On by default** per repo (`scanner_feed: false` to opt out) —
but a silent no-op until the GitHub App has the three alert read permissions.

### What to test

- [ ] **Permission check first**:
  `curl .../api/v1/ops-events?component=scanner_feed` — if you see
  `sources_unavailable` events, grant the GitHub App read on Code scanning /
  Dependabot alerts / Secret scanning, then confirm the events stop.
- [ ] On a GHAS-enabled repo, open a PR touching a file with an open alert →
  worker log `scanner_alerts_attached alerts=N` and the review mentions it.
- [ ] **Severity floor** (needs `b9675c0`): repo with an open secret-scanning
  alert → a finding on that same file must come out `critical` even if Claude
  rated it lower.
- [ ] Rate-limit behavior: under a GitHub 403 rate limit the review should
  retry (transient), not mark sources unavailable — hard to force manually;
  covered by `test_github_scanner_readers.py`, just don't be surprised by
  retries in logs.

**Automated coverage:** `worker/tests/test_scanner_feed.py`,
`test_github_scanner_readers.py`, `test_reviewer_scanner.py`.

---

## 5. Odoo Core Knowledge (`2b126b5`, `c0b7e5a`, `6bee527` core_sync)

**What it is:** operator-provisioned Odoo core/enterprise/docs worktrees under
`/core/<version>` + an AST-built registry (modules/models/fields, FTS-indexed
docs, greppable catalog markdown). Full/deep reviews and audits get the core
trees as `extra_dirs` + a `core_knowledge` param; diff reviews get computed
`core_overlap` hints (duplicate field / near-model); ticket analysis retrieves
docs via a Haiku query planner (`ticket_knowledge`). Per-repo
`odoo_version: "19.0"` in `.claude-review.yml`; per-instance `odoo_version`
via the instances API.

**Flags:** `REVA_CORE_KNOWLEDGE_ENABLED` (default false), `REVA_CORE_VERSIONS`,
`REVA_CORE_KNOWLEDGE_DIR=/core`. When enabled, **worker refuses to boot** if a
listed version isn't fully provisioned.

### What to test

- [ ] Prod provisioning already validated per the handoff (`/srv/reva-core/19.0`).
  Re-verify after any redeploy: env vars set, catalog non-empty,
  `curl .../api/v1/metrics | jq .core_knowledge` shows modules/sections counts.
- [ ] **Open item from the handoff:** the last ticket test logged
  `ticket_core_knowledge_unavailable`. Check whether the ticket's Odoo
  *instance* has `odoo_version` set
  (`SELECT name, odoo_version FROM odoo_instances;`) — the ticket path resolves
  the version from the instance, not the repo. Set it via
  `PATCH /api/v1/odoo-instances/{id}` and re-run an analysis; expect a
  "Retrieved Odoo knowledge" block influence and no
  `core_knowledge/version_unavailable` ops event.
- [ ] Review path: set `odoo_version: "19.0"` in a repo's `.claude-review.yml`,
  open a PR re-adding something core already has (e.g. a field that exists on
  the inherited model) → expect a `standard-functionality`/overlap hint in the
  review.
- [ ] Misconfig guard: worker with `REVA_CORE_VERSIONS=99.0` must fail boot
  loudly (only worth checking if you touch provisioning).

**Automated coverage:** ~10 `worker/tests/test_core_*` / `test_odoo_registry_*`
files, `api/tests/test_core_knowledge_api.py`.

---

## 6. Timesheet Wording Review (`ec68f1e`, `009d3f1`, `0dab5a0`)

**What it is:** Odoo POSTs up to 5000 timesheet lines to
`POST /api/v1/timesheet-review` (instance key). Worker reviews wording in
chunks of 100 (one strict tool call each): per line `ok` / `rewritten` (with
new text) / `needs_human` (with reason). Results → `timesheet_review_runs`/
`_lines` (metadata only — descriptions not stored at rest), callback →
`{base}/hr/timesheet-results`. Dedup on pending (instance, request_id) for
60 min. Instance budget/rate-limit apply. TUI: **Timesheets tab, key `-`**.
`009d3f1` capped Redis failure_ttl at 24h; `0dab5a0` made the TUI/API
instance-cost display include timesheet spend (the quota gate always did).

### What to test

- [ ] Direct API round-trip (no Odoo needed):
  ```bash
  curl -X POST .../api/v1/timesheet-review \
    -H "Authorization: Bearer <instance-key>" -H 'Content-Type: application/json' \
    -d '{"request_id":"t1","flagged_words":["urgent"],"lines":[{"line_id":1,
      "task_name":"Fix","project_name":"P","user_name":"U",
      "user_role":"consultant","description":"tried stuff, wasted time on the broken thing"}]}'
  ```
  → 202; poll `GET /api/v1/timesheet-review/{run_id}` (master key) → completed
  with `rewritten ≥ 1`. Check the rewritten text quality in the callback
  payload / Odoo.
- [ ] Same `request_id` again within 60 min → same run (dedup), no second job.
- [ ] Real batch from Odoo once the `/hr/timesheet-results` receiver is
  confirmed — watch for `odoo_callback/timesheet_results_failed` ops events.
- [ ] **Spend display** (`0dab5a0`): after a few runs, the TUI Odoo tab 24h/30d
  cost cells must include timesheet spend and match what the 429 budget gate
  uses. Before the fix these disagreed.
- [ ] A big batch (few hundred lines): completes chunk-by-chunk, cost sane,
  job_timeout not hit.

**Automated coverage:** `api/tests/test_v1_timesheet_reviews.py`,
`worker/tests/test_timesheet_*.py`, `tui/internal/ui/timesheets_test.go`.

---

## 7. Ops-Event Log (`6fe818c`, `c0b7e5a`, `b9675c0`)

**What it is:** the observability substrate everything above reports into.
Every caught-and-degraded error (codegraph, git timeouts, Odoo callback
failures, triage/core/scanner degradations) → `ops_events` row →
`GET /api/v1/ops-events?component=&severity=` → TUI Failures tab (**press `v`**
to toggle runs ↔ component events) + red `Degrade N events (24h)` dashboard
line. Retention `REVA_OPS_EVENTS_RETENTION_DAYS=30` (scheduler purge).

### What to test

- [ ] TUI tab 4 → `v` → events list renders; dashboard degrade line appears
  when events exist.
- [ ] Filters: `curl '.../api/v1/ops-events?component=odoo_callback&severity=error'`.
- [ ] This log is itself the *verification tool* for most features above —
  during all other tests, keep it open; anything degrading silently in logs
  but **missing** here is a review-blocking defect per CLAUDE.md.

**Automated coverage:** `api/tests/test_v1_ops_events.py`,
`worker/tests/test_ops_events.py`, `test_ops_hooks.py`.

---

## 8. Contract Tests + namespaced callbacks (`d1fe0f5`, `09bfca2`, `90e1092`)

**What it is:** all Odoo↔REVA payloads now flow through Pydantic contract
models; `contracts/` holds generated schemas/samples + `manifest.json` with a
`contracts_version` hash; drift tests fail if code and committed contracts
diverge. Callbacks are namespaced (`{base}/tickets/*`, `{base}/hr/*`) — stored
instance `callback_url` should be the **base** (`https://<odoo>/api/reva`);
legacy `/write-field`-suffixed rows are auto-stripped, no migration needed.

### What to test

- [ ] `python -m reva.odoo_contracts generate --check` → clean exit (run in a
  service venv). This is the only "test" the feature needs locally.
- [ ] After any contract-touching change: regenerate, commit, and
  `scripts/sync_contracts.sh <ast-odoo>` — CLAUDE.md now makes this part of
  definition-of-done.
- [ ] One legacy-URL instance (if any still store a full endpoint): ticket
  analysis still delivers to `…/tickets/write-field`.

---

## 9. Hardening batch + small features (`14c887e`, `dd6de48`, `6bee527`)

Mostly validated already (handoff doc); the durable behavioral changes worth a
one-time check:

- [ ] **Per-instance quotas**: `PATCH /api/v1/odoo-instances/{id}
  {"daily_budget_usd": 1, "rate_limit_per_minute": 2}` → exceed → 429 on
  create routes; TUI Odoo tab shows the columns.
- [ ] **Verifier default flip**: `REVA_VERIFY_FINDINGS` is now default **true**
  (was `REVA_VERIFY_HIGH_COST=false`) — blocking-threshold findings get a
  Haiku ground-check. If prod `.env` still sets the old var, it's dead; remove
  it. Watch `claude_spend` for the verify bucket.
- [ ] **Worker healthcheck / 2 API workers / 2 worker replicas**:
  `docker compose -f docker-compose.prod.yml ps` — workers `healthy`.
- [ ] **`GET /api/v1/health`** (`dd6de48`): master key → `authenticated_as:
  "master"`; instance key → `"instance"` + name; bad key → 401. This is the
  endpoint Odoo's "Test connection" button should use.
- [ ] **Strict tools** (`6bee527`): already prod-validated; regression signal
  is any `additionalProperties`/`maxItems` Claude 400 in logs → would mean a
  new tool schema bypassed `reva/tool_schema.py`.

---

## 10. Post-handoff fixes (2026-07-06, after the validation doc)

These landed *after* `docs/handoff-2026-07-06-prod-validation.md` was written —
they are **not yet deployed-validated** and are the freshest risk:

| Commit | Fix | How to verify |
|---|---|---|
| `c723de2` | Private-repo blobless clones: `git reset --hard` now carries the auth header (was `PermanentError: could not read Username` on first review of a new private repo) | Trigger a review on a **fresh, never-cached private repo** PR → completes; no `promisor` errors in worker log. This bug killed Cloudunify/bsvs PR #5 — rerun that flow. |
| `e2a0b7d` | Planner strict schema requires *every* issue field (ACs/type could silently vanish); prompt defaults to ONE issue, English ACs, structured body | Create issues from a sparse German ticket → one issue, English ACs present, What/Why/Expected-behaviour body. Rich multi-part ticket → epic + children (per handoff test 5). |
| `0181afe` + `def127e` | Analyzer max_tokens 8192→16384; truncation named honestly; malformed output retried **once** in-process before failing (ops event `malformed_output_retried`) | Run analyses on a long/complex ticket. If a retry happens: warning ops event + still `completed`. A hard failure must say `truncated at max_tokens=16384`, never `summary: Field required`. |
| `cbd92dc` | TUI Tickets tab: newest analysis wins (was oldest); issues merged across runs (re-plans no longer hide older created issues) | Ticket with failed→resent analysis shows the newest; ticket with a feedback re-plan still lists **all** its issues + epic. |
| `8c5c7a2` | TUI Odoo tab: `D` deletes instance (y/n confirm, detaches history, drops change_notes); new-key banner only dismisses on esc/enter | Create a throwaway instance, `D` → `n` cancels, `D` → `y` deletes (2nd DELETE curl → 404). After `ctrl+r` rotate, mash a random key — banner must stay until esc/enter. |
| `3fb91b1` | TUI Tickets tab: `e` on a row with a **failed issue run** requeues the issue run (resumes plan, creates only missing issues); otherwise requeues the analysis as before | On a failed create-issues run press `e` → status line `issues run #N requeued` (not `analysis …`); missing issues get created, existing ones aren't duplicated. |

---

## Suggested test order (one afternoon)

1. **Deploy** current main; run handoff doc tests 1–2 (health + TUI smoke).
2. **Tickets pipeline** (handoff tests 3–6 + post-handoff fixes): analysis →
   `result_structured` in DB → create issues (sparse + rich ticket) → TUI merge/
   requeue behaviors (`e`, newest analysis).
3. **Loop closure**: PR with `Closes #N` → AC-grounded review log line → merge
   → `change_notes` row → close all issues → `✔`/ready counter. Expect the two
   `*_rejected` ops-events (Odoo receivers missing) — that's the visible-no-op
   contract working.
4. **Fresh private repo review** (`c723de2`) — highest-risk regression fix.
5. **Value report** manual enqueue for June; review the markdown.
6. **Triage**: enable flag, one security-shaped PR + one trivial PR.
7. **Scanner feed**: check ops-events for `sources_unavailable` → fix App
   permissions if needed.
8. **Timesheet** curl round-trip + spend display check.
9. Throughout: Failures tab `v` view — every degradation you provoke must
   appear there.
