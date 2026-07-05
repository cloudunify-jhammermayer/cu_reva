# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Working principles

### 1. Think Before Coding
Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.
- **Treat docs as possibly stale.** Claims in `HANDOFF.md`, `FEATURE_ROADMAP.md`, `docs/`, and READMEs about "known bugs", TODOs, "next steps", planned fixes, or line numbers can lag the code. Verify against the current code (and its tests) before acting — don't "fix" something already fixed. When you find a doc contradicted by the code, correct the doc as part of your change. **Code and tests win over prose.**

### 2. Simplicity First
Minimum code that solves the problem. Nothing speculative.

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes
Touch only what you must. Clean up only your own mess.

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution
Define success criteria. Loop until verified.

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

### 5. Keep the TUI in Sync
The Go/Bubble Tea **tui** (`tui/`, read-only client of `/api/v1`) is the operational dashboard. When a change adds data worth seeing at a glance — feedback/learning signals, mutes, finding outcomes, per-repo/per-worker stats — surface it there too, adding or extending the backing `/api/v1` endpoint as needed. Match the existing tab/client patterns (`internal/ui/*.go`, `internal/api/{client,iface,mock,types}.go`); `go build/vet/test ./...` must stay green. Don't leave new capabilities visible only in the DB or logs.

## What this is

REVA — automated GitHub PR review platform built on Claude. Webhook-driven: it debounces PR pushes, reviews the change with a headless Claude Code CLI against a local repo clone, and posts a Check Run + PR Review with inline comments. Also: whole-repo audits, Odoo ticket analysis, and replies to developer questions on its own inline comments.

Authoritative docs: root `README.md`, per-directory `README.md` files, `docs/` guides and `docs/superpowers/specs/`. `HANDOFF.md` is the current work handoff / resume point — read it when resuming work.

## Commands

```bash
# Local stack (api on :8080)
make dev                  # docker compose up        (make dev-build to rebuild)
make logs-worker          # also: logs, logs-api, logs-scheduler
make psql                 # psql into the dev Postgres

# Python tests — per-service venvs, Python 3.14, each installs reva/ as editable
cd worker && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/                       # same pattern for api/ and scheduler/
.venv/bin/python -m pytest tests/test_runner.py -k name # single test
make test                 # all three services (uses the existing .venvs)
make test-integration     # real-Postgres concurrency tests (throwaway container)

# Lint / type-check (CI: ruff blocking, mypy advisory)
ruff check reva worker/worker api/app scheduler/scheduler
mypy reva worker/worker api/app scheduler/scheduler --ignore-missing-imports

# Go TUI
cd tui && go test ./...
cd tui && go run . --demo   # demo mode, no live server needed
```

Unit tests need no Docker or network: SQLite in-memory replaces Postgres, `httpx` MockTransport fakes GitHub, subprocess mocks fake the Claude CLI. Concurrency behavior (`FOR UPDATE SKIP LOCKED`, advisory locks) is a no-op on SQLite — that's what the `REVA_TEST_POSTGRES_URL`-gated integration tests in `make test-integration` cover.

Tests build tables from the ORM models (`create_all`), **not** the SQL files in `db/migrations/` — so a migration's raw SQL and any Postgres-only query construct (e.g. `count(distinct case(...))`) are exercised only on real Postgres. Validate those via `make test-integration` or the first staging boot.

**Definition of done before committing a feature:** the suites for every service you touched are green, plus `ruff`. A change to shared `reva/` affects all three services — run `worker`, `api`, **and** `scheduler` (`make test`). Touching `tui/` requires `cd tui && go build ./... && go vet ./... && go test ./...`. State outcomes honestly: if a path is only unit-tested (not live-CLI / not Postgres), say so.

## Architecture

Pipeline: GitHub webhook → **api** (FastAPI, verifies HMAC signature, upserts `pending_reviews`) → **scheduler** poller (consumes due rows after the 10-min debounce, enqueues RQ) → **worker** (clones the repo at head SHA, runs the headless `claude` CLI, posts Check Run + PR Review) → Postgres for analytics. The Go/Bubble Tea **tui** is a read-only client of the internal `/api/v1` (Bearer `REVA_API_KEY`).

Two Claude integration paths — don't mix them up:

- **Headless Claude Code CLI** (`reva/claude_code_runner.py`) — all PR review modes and repo audits. Runs against a local clone under `REVA_REPO_CACHE_DIR` so Claude can read connected files. Output contract: the `submit_review` tool schema written to a temp JSON file inside the clone.
- **Messages API** (`reva/claude_client.py`) — structured/fast paths: Odoo ticket analysis and inline-comment reply answers. Prompts assembled by `reva/prompt_builder.py` from `prompts/*.md` with prompt-cache–controlled blocks.

Components:

- `reva/` — shared library installed editable into every Python service: Pydantic types (`types.py`: `Finding`, `ReviewResult`, `ReviewMode`, `RepoConfig`), GitHub client (App JWT → installation tokens), diff filtering (`diff_utils.py`), Check Run / review formatting, finding ground-checking (`finding_verifier.py`), core-knowledge layer (`odoo_registry.py`, `core_knowledge.py`, `ticket_knowledge.py` — operator-provisioned `/core` worktrees + registry), Google Chat notifications, cost estimation, DB layer (`reva/db/`: engine + migrations, ORM models, transactional writers).
- `worker/` — RQ jobs: review, audit, ticket_analysis, ticket_issues, comment_reply, weekly_report, repo_cache_eviction. `Reviewer.execute()` is the pure pipeline (token → diff → config → CLI → parse → cap at 15 findings); `runner.run_review()` wraps it with claim/persist/post/notify. Retries: `TransientError` retried by RQ (max 3, backoff); `PermanentError` fails and notifies.
- `scheduler/` — single-replica loops: debounce poller, weekly reporter, stale-`running` reaper, operational alerts, repo-cache eviction.
- `prompts/skills/` — the headless-CLI skills. Selection is centralized in `Reviewer._select_skill` on the final (post-filter) diff, precedence **migration > delta > xml-only > diff/full**: `reva-diff-review` (diff/diff-all), `reva-full-review` (full/deep), `reva-delta-review` (incremental), `reva-migration-review` (Odoo upgrade scripts — overrides mode/delta, keeps `delta_base_sha`), `reva-xml-review` (XML-only diff), `reva-repo-audit` (audits). CodeGraph MCP (`REVA_CODEGRAPH_ENABLED`, fail-silent) is wired into the repo-aware skills only (full/deep/audit), never the diff-depth paths.
- `db/migrations/` — plain SQL, applied idempotently at service startup by `Database.migrate()` under a Postgres advisory lock. Conventions for a new table: numbered file, idempotent (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`), `id BIGSERIAL PRIMARY KEY` (match the existing files — not `GENERATED … IDENTITY`), and add the matching ORM model in `reva/db/models.py` (tests build from the models, so a missing model means the table is invisible to tests).

Invariants the design leans on (look for `SECU-*` / `CONC-*` / `CORR-*` codes in comments and follow that convention):

- **Idempotency everywhere.** `review_runs` unique on (repo, pr, sha, mode); workers claim atomically via `claimed_by_job_id` + `FOR UPDATE`; `is_already_posted()` makes RQ retries skip completed work (explicit comment/requeue triggers bypass this); webhook deliveries dedup on `delivery_id`; audit issues dedup via hidden markers in the issue body.
- **Debounce semantics.** `pending_reviews` unique on (repo, pr_number) — rapid pushes replace the row and push `scheduled_at` forward. Mode precedence: diff < diff-all < full < deep.
- **Cost control.** Rolling 24-h budget cap (`REVA_DAILY_BUDGET_USD`) checked under an advisory lock before any paid call; token usage and estimated USD persisted per run.
- **Scope filtering.** Only `custom_addons/` / `custom-addons/` paths reviewed by default (`reva/diff_utils.py`); `.po`/`.pot`/`.md`/`.rst` and `odoo/`/`enterprise/` always stripped — but `custom_addons/**/*.xml` **is** reviewed (Odoo views, via `reva-xml-review`). Per-repo overrides in `.claude-review.yml` (`max_diff_lines`, `max_diff_tokens`, `max_xml_diff_lines`, `max_xml_diff_tokens`, `skip_paths`, `review_all_paths`, `block_on_severity`, `verify_findings`, `odoo`, `custom_instructions`).
- **Untrusted content is fenced.** Repo file content is nonce-wrapped before being shown to Claude (prompt-injection guard); internal paths are redacted from anything posted to GitHub; secrets follow the `NAME` / `NAME_FILE` convention (`reva/config.py: env_or_file()`).
- **Degradations are visible.** Any error a component catches and degrades around (CodeGraph fallback, callback failure, retrieval miss, git retry) must both log AND `writers.record_ops_event(...)` — surfaced via `GET /api/v1/ops-events` and the TUI Failures tab. Silent `except: log-and-continue` without an ops event is a review-blocking defect in new code.
- Model selection lives in one place: `reva/config.py` (`REVA_DEFAULT_MODEL` for diff/full/tickets/replies, `REVA_DEEP_MODEL` for deep reviews and all audits).
