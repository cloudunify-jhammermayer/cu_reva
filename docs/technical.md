# REVA — Technical companion

The same feature set as [`user.md`](user.md), explained at the systems level:
how each feature works, and what to expect in cost, latency, and failure
behavior. Deep-dives live in the per-directory `README.md`s and `docs/`;
design history in `docs/superpowers/specs/`.

## Architecture

```
GitHub webhook ──▶ api (FastAPI)          verifies HMAC, upserts pending_reviews
                      │
                      ▼
                scheduler                 debounce poller (10 min), weekly report,
                      │                   stale-run reaper, operational alerts
                      ▼  RQ (Redis)
                   worker                 clones repo @ head SHA, runs Claude,
                      │                   posts results, sends Odoo callbacks
                      ▼
                 PostgreSQL               every run, finding, token count, USD
                      ▲
        tui (Go/Bubble Tea) ── read-only /api/v1 (Bearer)
```

Two Claude integration paths — different tools for different shapes of work:

| Path | Used for | Why |
|---|---|---|
| **Headless Claude Code CLI** against a local repo clone | All PR review modes, repo audits | The model can *read the repo* (callers, models, views), not just the diff. Output contract: the `submit_review` JSON schema. |
| **Messages API** with prompt caching | Ticket analysis, issue planning, timesheet review, comment replies | Structured, fast, cheap; tool-use schemas enforce the output shape. |

Model selection is central (`reva/config.py`): Sonnet (`REVA_DEFAULT_MODEL`) for
diff/full reviews and all Odoo paths, Opus (`REVA_DEEP_MODEL`) for `/deep-review`
and audits.

## PR review engine

- **Debounce**: pushes upsert one `pending_reviews` row per PR; `scheduled_at`
  moves forward on every push; the scheduler enqueues after 10 quiet minutes.
  Comment triggers (`/review` etc.) skip the debounce. Mode precedence when
  triggers race: diff < diff-all < full < deep.
- **Scope filter** (`reva/diff_utils.py`): only `custom_addons/` paths by default;
  translations/docs and `odoo`/`enterprise` always stripped; `custom_addons/**/*.xml`
  *is* reviewed. `review_all_paths: true` widens to the whole repo (non-Odoo repos).
  Oversized diffs (default > 2 500 lines / > 60 000 estimated tokens) are declined
  with an explanatory comment, not silently skipped.
- **Skill selection** (one place: `Reviewer._select_skill`, on the post-filter
  diff): migration scripts > incremental delta > XML-only > diff/full. Incremental
  reviews re-review only the change since the last reviewed SHA.
- **Findings pipeline**: parse → optional second-pass self-critique
  (`verify_findings`: a ground-checking pass that re-reads the code and drops
  unsupported findings) → category mutes → cap at 15 → Check Run conclusion from
  `block_on_severity` → PR review with inline comments. Internal paths are
  redacted from anything posted to GitHub.
- **Learned memory**: `/dismiss`, `/mute`, and fix-detection accumulate per-repo
  signals that are injected into future review prompts, tuning out repeated
  false positives per repo (kill switch: `learned_memory`).
- **Requirements check** (`intent_check`): linked issues = GraphQL
  `closingIssuesReferences` ∪ closing-keyword refs from the PR body; issue bodies
  are fetched, **nonce-fenced** (prompt-injection guard), and passed as
  `stated_intent`. The model returns per-issue verdicts, which are filtered
  (only resolved refs, deduped, hallucinated numbers dropped, notes sanitized
  and capped) and rendered as an advisory section. Structurally advisory: the
  Check Run conclusion function never sees them.
- **Idempotency**: `review_runs` unique on (repo, PR, SHA, mode); workers claim
  rows atomically; RQ retries skip already-posted work. A crashed worker's
  `running` row is reaped and marked failed by the scheduler.

## Repository audits + CodeGraph

Audits run the whole default branch through the deep model. Major/critical
findings become GitHub issues (deduped by hidden markers in the issue body;
label `reva-audit`). With `REVA_CODEGRAPH_ENABLED`, a pre-built code-graph MCP
server is exposed to repo-aware skills (full/deep/audit) — fail-silent: indexing
problems degrade to a graph-less review, never a failed one.

## Odoo integration

- **Instance registry**: each Odoo instance is registered with its own inbound
  API key (SHA-256 lookup), callback URL + key (Fernet-encrypted at rest),
  per-instance daily budget and rate limit. Create routes authenticate the
  instance; the per-run poll/requeue routes accept the instance key scoped to
  that instance's rows (Odoo's self-heal uses these) or the master key.
- **Contracts**: every Odoo↔REVA payload is a Pydantic model; JSON Schemas +
  samples are generated (`python -m reva.odoo_contracts generate`), version-
  hashed, vendored into ast-odoo (`scripts/sync_contracts.sh`) and pinned by
  tests on both sides — wire drift fails CI, not production.
- **Ticket analysis**: Messages API with a fixed tool schema; result HTML is
  written back via the `write-field` callback. Attachments (docx/pdf/txt/md) are
  text-extracted and analyzed with the description. Can escalate to the
  headless CLI — see *Planner-gated code grounding* below.
- **Golden estimates** (spec 2026-08-04): `prompts/golden_estimates.yml` is the
  single calibration source for every development estimate — an
  operator-curated file of real closed tickets with trusted hours, checked in
  and hand-edited, REVA never writes to it. It replaces three hand-maintained
  copies of the same four bands (config/small/medium/large) at all three
  estimating sites: `ticket_analysis.md` §5 and `ticket_issues.md` substitute
  `{{ESTIMATE_CALIBRATION}}` on the Messages-API path
  (`reva/ticket_analyzer.py`, `reva/ticket_issue_planner.py`); the escalated-CLI
  skill `skills/reva-ticket-analysis.md` gets the same block via
  `ClaudeCodeRunner.review()`'s `skill_vars` parameter, deliberately **not**
  `skill_params` — everything in `skill_params` is nonce-fenced and framed to
  the model as "DATA, not instructions," which would demote binding
  calibration to data instead of trusted prompt text. `StoryEstimate` and
  `TicketIssueItem` gain `anchor_ref` (`"<anchor-id>#<story-id>"`) and
  `complexity_drivers` (a fixed enum, ≤3 per story); `anchor_confidence` is
  never the model's own judgement — code overwrites it from the Jaccard
  overlap between the story's drivers and the cited anchor's, gated on
  matching `kind` (`reva/golden_estimates.py: score()`: both driver sets empty
  → `high`; overlap ≥ 0.6 → `high`; overlap > 0 → `medium`; otherwise, or no
  anchor resolved → `low`). All three fields are **internal only** — an
  anchor names another customer's ticket, so none of them reach the Odoo HTML
  field, a GitHub issue body, or the Projects board; those renderers emit
  explicit fields rather than serialising the whole model, and tests pin that
  boundary. A file with no anchors renders a bands-only block — today's
  behaviour — so the feature is inert until anchors are written. Kill switch
  `REVA_GOLDEN_ESTIMATES` (default on) forces bands-only regardless.
- **Support answers** (spec 2026-07-25): Odoo asks a question via
  `POST /api/v1/support-request`; REVA drafts an answer into an HTML field for
  a **consultant to review and send** — it is never posted to the customer.
  Threads and turns live in `support_threads` / `support_turns` (migration
  044); REVA owns the turn history and replays it, while Odoo re-sends the full
  chatter snapshot each turn (delta bookkeeping drifts the moment a message is
  edited or deleted, with no cheap way to detect it). Chatter entries marked
  `visibility: "internal"` are fenced separately with a never-quote
  instruction: they routinely hold the real answer, so they must inform the
  draft without ever surfacing in it.
- **Personas** (migration 043, `reva/persona.py`): per-project tone, resolved
  **per field** — a `default` row, overlaid by a repo row's non-NULL knobs,
  plus additive `persona_context` from the request that never overrides a knob.
  `content_policy` is a separate column from `style_notes` so it renders as a
  hard constraint rather than tone advice. An inactive row resolves as absent,
  so deactivating actually falls back. Master-key API only — an Odoo instance
  must not be able to change what REVA says to its own customer. The resolved
  block is deterministic because it sits in a `cache_control` prompt prefix.
- **Planner-gated code grounding** (shared by ticket analysis and support
  answers): `plan_core_queries` returns `needs_repo_code` alongside its search
  terms, carried on `TicketKnowledge` and **independent of `worth_checking`** —
  a question can need the project's code while the official docs are
  irrelevant. True runs one headless-CLI pass (`reva-ticket-analysis` /
  `reva-support-answer`) under `repo_lock` against the worker clone; false stays
  on the Messages API. Escalation is roughly 10-30x the cost and takes the lock,
  so the gate shuts on any doubt — planner failure, no `github_url`, App not
  installed, kill switch — and every shut reason is an ops event, because a
  silently ungrounded answer looks exactly like a well-grounded one. Brakes:
  `REVA_TICKET_CODE_GROUNDING`, per-repo `code_grounding`. The ticket skill's
  load-bearing rule is that code is **evidence, never output**: the analysis is
  written for a product owner, so no field, method, view, or file path may
  appear in it.
- **Ticket knowledge grounding** (one cheap planner call feeds two retrievals,
  `reva/ticket_knowledge.py`): a small-model planner derives English search
  terms from the ticket; those terms retrieve (a) sections of the official
  **Odoo docs + core registry** (operator-provisioned `/core` worktrees,
  version-matched to the instance) backing the *Standard Odoo Coverage* output
  section, and (b) sections of the **customer repo's own custom-addon markdown
  docs** backing the *Existing Customizations* section (spec 2026-07-14). Repo
  docs are indexed lazily into Postgres (`repo_doc_sections`, FTS-ranked;
  `reva/repo_docs.py`): at analysis time the worker resolves the repo's
  **default branch** from `TicketJobParams.github_url`, compares the git-tree
  SHA against the stored sync state (`repo_docs_sync`) and re-indexes only on
  change — the common case is two GitHub API calls and zero file fetches.
  Concurrent syncs of one repo are serialized by a per-repo advisory lock
  (loser skips and reads the current index); caps: 50 files, 2 000-char
  sections, 8 injected. Both retrieval blocks are reference-data-framed; the
  repo-docs block is additionally **nonce-fenced** (repo-authored content).
  Every degradation (invalid URL, uninstalled app, sync/search failure) falls
  back to a narrower analysis with a `repo_docs`/`core_knowledge` ops event —
  never a failed run. The injected-section count is persisted per analysis
  (`repo_docs_sections_used`) and shown in the TUI Tickets tab.
- **Issue creation**: Claude plans 1–10 issues + epic (schema-enforced, incl.
  per-issue `estimate_hours`); creation is resumable — the plan is persisted, a
  retry creates only what's missing, and callbacks always send the **union** of
  issues across all of the record's runs (dedup by number). Callbacks carry
  per-issue state, plan/complete dates, estimates, and a `total_estimate_hours`.
- **Projects v2 board**: fail-soft projection (a board failure never fails the
  run): Due date (DATE), Estimate (NUMBER), Priority (single-select), Status →
  Todo. Board status sync is a separate worker job: PR opened/reopened/ready →
  "In Progress", review completed → "In review" — only for cards REVA created,
  only between existing options, guarded against merged/closed PRs, kill switch
  `board_status_sync`.
- **Lifecycle sync**: issue webhooks update Odoo (issue-state callback with the
  full snapshot); all-closed sends the `ready` signal; merged linked PRs send a
  change-note callback. REVA never closes/completes the ticket itself.
- **Timesheet review**: batch endpoint; each line is kept / reworded / flagged
  for a human, returned via callback with per-line reasoning.

## Ops & observability

- **Degradations are visible by invariant**: anything caught-and-degraded must
  log *and* write an ops event (`GET /api/v1/ops-events`, TUI Failures tab).
- **TUI**: read-only client of `/api/v1`; 12 tabs incl. per-ticket journey
  timeline, feedback/mute state, per-instance spend; failed runs requeue with
  one key. Demo mode (`go run . --demo`) needs no server.
- **Notifications**: Google Chat on permanent failures (with error taxonomy for
  Claude/GitHub/config errors) and for the weekly report; scheduler alerts on
  queue depth, failed jobs, and repo-cache disk.
- **Reports**: weekly (cost, findings, durations, per-repo/author); monthly
  value report available (chat delivery off by default).

## Security & data handling

- Webhooks: HMAC signature verification; deliveries deduped by `delivery_id`.
- API: master key (ops/TUI) vs per-instance keys (Odoo) vs shared scoped routes;
  fail-closed when auth is required but unconfigured; nginx + per-key rate limits.
- Prompt safety: repo file content, issue bodies, and customer-repo doc
  excerpts are nonce-fenced before the model sees them; internal paths redacted
  from GitHub-bound output; secrets via `NAME`/`NAME_FILE` convention.
- Retention: raw customer ticket text (and Claude-rendered derivatives of it) is
  scrubbed after 30 days (`REVA_TICKET_TEXT_RETENTION_DAYS`); consultant files
  are never stored server-side.
- Cost: rolling 24-h budget caps (global + per instance) are checked under an
  advisory lock *before* any paid call — over budget means declined, not queued.

## Expected costs & latency (production data, 2026-07-10)

| Job | Runs so far | Avg cost | Max seen | Avg duration |
|---|---|---|---|---|
| PR diff review | 193 | **$0.92** | $4.50 | ~4 min (after the 10-min debounce) |
| PR diff review, all paths | 10 | $0.55 | $1.36 | ~3.5 min |
| Ticket analysis | 30 | **$0.04** | — | well under a minute |
| Issue creation run | 34 | $0.03 | — | ~1 min incl. GitHub calls |
| Repo audit (Opus) | 1 | n/a (cost not itemized) | — | ~5 min, 8 findings |

Rules of thumb: a working day of normal PR traffic costs a few dollars; the
Odoo paths are effectively free; deep reviews/audits on Opus cost several times
a diff review — that's why they're manual-trigger only. Feedback commands
(`/dismiss`, `/mute`) cost nothing; a question replied to an inline comment is
one small Messages-API call.

## Expected limits & failure behavior

- **Caps**: ≤ 15 findings per review; diff size caps decline with an explanation;
  1–10 issues per creation run.
- **Retries**: transient errors (rate limits, 5xx, network) are retried by RQ
  with backoff (max 3); permanent errors fail the run, notify Google Chat, and
  appear in the Failures tab with one-key requeue. Retried jobs resume
  idempotently — a callback-only failure never re-pays for the Claude call.
- **Advisory vs blocking**: only finding severity ≥ `block_on_severity` fails
  the Check Run. Requirements check, board sync, change notes, estimates are
  advisory/additive and individually switchable per repo.
- **Fail-soft surfaces**: board projection/status, CodeGraph, change notes,
  ticket knowledge retrieval (core + repo docs), and callbacks degrade without
  failing the paid work — always leaving an ops event.
- **Known boundaries**: reviews see the repo at the head SHA (not your IDE
  state); the requirements check only sees *linked* issues; estimates are
  low-end AI-assisted figures for planning, not commitments; board "In review"
  requires that column to exist (REVA won't create status options).
