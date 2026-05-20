# REVA — Implementation Handoff

Persistent context for agents picking up this project. Read this **before**
touching code or docs. Updated: 2026-05-20 (after slice 9a).

---

## Project at a glance

REVA (Review & Evaluation Agent) is an automated GitHub PR reviewer powered
by the Claude Messages API. Architecture is fully documented in `doc/00`
through `doc/13` plus `doc/pr-review-requirements.md`. The current working
directory is **not** a git repo yet.

Tech stack (locked): FastAPI + Redis/RQ + Postgres 16 + Claude API +
Bubble Tea TUI, deployed via Docker Compose on Hetzner.

**Python version: 3.14 everywhere** — production image is `python:3.14-slim`
(Dockerfile) and local dev must use 3.14 too. On macOS install via
`brew install python@3.14`; the project venv is created with that interpreter.

## Slices completed

- **Slice 1** — worker scaffold, contracts, doc cleanup (model IDs, tool_use, caching, REVA rename in doc/00).
- **Slice 2** — `ClaudeClient.review` implemented + 12 pytest cases passing on 3.14.
- **Slice 3** — `Reviewer.execute` implemented (pure orchestration) + `custom_instructions` cached block + `skip_paths` decline-all helper + risk_level recomputation + 20 more pytest cases (32 total).
- **Slice 4** — `GitHubClient` (read-only) implemented: JWT auth, installation-token cache, PR / diff / changed-files / file-content reads, GitHub-specific error mapping. 19 more pytest cases (51 total).
- **Slice 5** — DB layer: SQLAlchemy 2.0 typed declarative models for all 9 tables, plain-SQL migration runner with `schema_migrations` tracking, `RepoLookup` adapter satisfying Reviewer's Protocol, writers for review_run lifecycle + upserts for repository/pull_request/pending_review/github_event. 17 more pytest cases (68 total) on SQLite-in-memory.
- **Slice 6** — GitHub *poster* (write surface): `create_check_run`, `create_pr_review`, `create_issue_comment` on `GitHubClient`; shared `_github_http.py` for error mapping (used by reader and writer); pure `review_formatter.py` with conclusion matrix + Check Run output + PR body + inline-comment + decline templates; diff hunk parser (`parse_diff_hunks`, `find_line_in_hunks`) + `split_findings` for inline-vs-unmapped partitioning. 30 more pytest cases (98 total). Doc 08 ARIA→REVA renamed.
- **Slice 7** — end-to-end wiring: `Settings` (env-loaded), `WorkerContext` + `build_worker_context`, `run_review` orchestrator with idempotent retry (skip post if `check_run_id` already set), per-status posting paths (completed → PR review + check run; declined → issue comment + neutral check; stale → skipped check; failed → failure check with best-effort posting). `main.py` builds the context and starts RQ; `tasks.run_review` is a thin re-export so the import path stays stable. 10 more pytest cases (108 total).
- **Slice 8** — `prompts/` content: `system.md` (REVA identity, anti-injection guard, tool_use contract, severity/category definitions, rules), `diff_review.md`, `deep_review.md`, `odoo19.md`, `CHANGELOG.md` (v1.0). 8 sanity-tests confirming `PromptBuilder` loads the real files (116 total). Per-directory README.md added to root, doc/, db/migrations/, prompts/, worker/, worker/worker/db/, worker/tests/.
- **Slice 9a** — extracted reusable building blocks into `shared/reva/` as an installable package (`reva-shared`, pyproject.toml). Moved 11 modules + the `db/` subpackage. Rewrote all imports inside `shared/`, `worker/`, and `worker/tests/` (`from worker.X` → `from reva.X` for moved modules). `worker/conftest.py` now adds both `worker/` and `shared/` to `sys.path`; `worker/requirements-dev.txt` adds `-e ../shared`. Updated `worker/Dockerfile` to install the shared package first. **All 116 existing tests still pass** — refactor was lossless.

### Files created (current layout after slice 9a)

```
shared/                              ✅ installable `reva-shared` package
├── pyproject.toml
├── README.md
└── reva/
    ├── __init__.py                 version stub
    ├── types.py                    ✅ Finding, ReviewResult, JobParams, ClaudeResponse, ContentBlock
    ├── errors.py                   ✅ WorkerError / Transient / Permanent / StaleHead / Declined
    ├── review_tool.py              ✅ submit_review tool schema derived from pydantic
    ├── claude_client.py            ✅ httpx POST + tool_use parse + cache-token accounting + status mapping
    ├── prompt_builder.py           ✅ build_system_blocks returns cache-tagged blocks (file IO works)
    ├── cost.py                     ✅ pricing table + estimate_cost (placeholder rates)
    ├── diff_utils.py               ✅ count_diff_lines + estimate_diff_tokens + iter_diff_files + parse_diff_hunks + find_line_in_hunks
    ├── github_client.py            ✅ Read + write surface: JWT auth, installation-token cache, reads + writes, shared error mapping
    ├── _github_http.py             ✅ map_github_status + NotFound + retry-after parsing
    ├── review_formatter.py         ✅ compute_check_conclusion + format_* + severity emoji + split_findings
    └── db/                         ✅ Postgres layer — own README
        ├── __init__.py
        ├── engine.py               ✅ create_engine_from_url, Database facade, migrate() runner
        ├── models.py               ✅ 9 SQLAlchemy 2.0 typed declarative models; SQLite-friendly PK variant
        ├── repo_lookup.py          ✅ DatabaseRepoLookup adapter
        └── writers.py              ✅ idempotent record_review_* + upserts + record_github_event + is_already_posted

worker/                              ✅ RQ worker — orchestration glue only
├── Dockerfile                      installs shared/ first, then worker requirements
├── requirements.txt                worker-only: rq, redis
├── requirements-dev.txt            adds -e ../shared and pytest
├── README.md
└── worker/
    ├── __init__.py
    ├── reviewer.py                 ✅ pure Reviewer + GitHubReader/RepoLookup Protocols
    ├── runner.py                   ✅ WorkerContext + build_worker_context + run_review
    ├── tasks.py                    ✅ stable enqueue path (re-exports run_review)
    ├── settings.py                 ✅ frozen Settings dataclass; from_env classmethod
    └── main.py                     ✅ load Settings → build context → start RQ worker

db/migrations/                       ✅ Postgres DDL applied at startup
├── 001_initial.sql
├── 002_feedback.sql
└── 003_prompt_tracking.sql

worker/
├── requirements-dev.txt            ✅ pytest (and -r requirements.txt)
└── tests/
    ├── __init__.py
    ├── conftest.py                 ✅ adds worker/ to sys.path
    ├── fixtures/
    │   └── successful_review.json  ✅ recorded Anthropic tool_use response
    ├── test_claude_client.py       ✅ 12 cases, httpx MockTransport, no live API
    ├── test_reviewer.py            ✅ 20 cases with in-memory fakes for GitHub / RepoLookup / Claude / PromptBuilder
    ├── test_github_client.py       ✅ 25 cases (read + write); session-scoped RSA fixture; JWT verified against the public key
    ├── test_db.py                  ✅ 17 cases on sqlite:///:memory:; covers writers, upserts, idempotency, migration runner
    ├── test_diff_utils.py          ✅ 7 cases for hunk parsing + line lookup
    ├── test_review_formatter.py    ✅ 17 cases: conclusion matrix, body templates, severity emoji, decline message
    └── test_runner.py              ✅ 10 cases: completed (mapped + unmapped), no-findings, declined, stale, permanent error, transient error, idempotent retry, persistence, Settings.from_env validation
```

Legend: ✅ = functional, 🟡 = interface locked but body stubbed.

### Docs updated in this slice

- `doc/00-overview.md` — REVA name locked (ARIA placeholder removed from this doc only).
- `doc/06-review-worker.md` — model IDs → `claude-sonnet-4-6` / `claude-opus-4-7`; pricing table includes cache rates; new sections for tool_use, prompt caching, and RQ-owned retries; `parse_response` rewritten to read `tool_use_input`.
- `doc/07-claude-prompts.md` — output contract rewritten for tool_use; `is_odoo_specific` added to example; new Prompt Caching section; assembly steps reflect content-block list.
- `doc/pr-review-requirements.md` — rules #11 (tool_use mandatory) and #12 (token-based diff guard, default 60k tokens) added.

## Architectural decisions (locked)

| Decision | Choice | Reasoning |
|---|---|---|
| Structured output | **tool_use with `submit_review`** | Schema derived from pydantic ReviewResult — no regex JSON parsing, no markdown-fence failure mode. |
| Prompt caching | **Enabled from day one** | system.md + odoo19.md + CLAUDE.md tagged with `cache_control: ephemeral`. User message (diff) not cached. Cuts input cost ~90% on repeated reviews of the same repo. |
| Retries | **RQ-level only** | `tasks.run_review` enqueued with `rq.Retry(max=3, interval=[30,120,300])`. Claude client raises Transient/Permanent — no internal retries. |
| Reviewer boundary | **Pure** | `Reviewer.execute(params) -> ReviewResult`. No DB writes, no GitHub posting, no notifications inside Reviewer. Side effects live in `tasks.run_review`. |
| Default models | Sonnet 4.6 default, Opus 4.7 for `/deep-review` | Confirmed by user. |
| Agent name | **REVA** — Review & Evaluation Agent | Confirmed by user. |
| Python version | **3.14** everywhere (Dockerfile + local venv) | User: "newest python version". |
| `risk_level` source | **Recomputed by worker** after capping (per pr-review-requirements §4). | Resilient to the 15-finding cap dropping a critical finding. |
| Bad `.claude-review.yml` | **Log + empty config + proceed** | Typo shouldn't block all PRs in a repo. |
| Invalid Claude finding | **PermanentError** (no salvage) | Matches pr-review-requirements §5 "validation failure" rule. |
| `skip_paths` (current scope) | Decline only if **all** changed files match. Per-hunk filtering deferred. | Cheap, safe, lets reviews ship. |
| `custom_instructions` | Appended as a 4th cached system block. | Matches doc/12 contract. |
| GitHub App PEM input | Passed as a **string** at GitHubClient construction. | Caller (eventually `tasks.run_review` / settings) owns disk reads. Keeps the client testable without filesystem. |
| Installation-token cache | In-memory on the GitHubClient instance, 5-min safety margin under GitHub's reported `expires_at`. | One token mint per installation per worker process per hour; dies with the worker. |
| changed_files pagination | 30 pages × 100 files = 3000 file safety cap. | Aligned with the 1000-line size guard plus headroom. |
| ORM | **SQLAlchemy 2.0 typed declarative.** User picked this over the leaner psycopg+raw-SQL alternative. | Conventional Python pattern; one schema definition in `models.py` is the source of truth. |
| Sync vs async | **Sync everywhere.** | RQ workers are sync; FastAPI internal API can use sync sessions via threadpool. |
| Migration runner | **At process startup**, idempotent. Tracks state in `schema_migrations`. Plain SQL files (no Alembic). | Both worker and api call `Database.migrate()` on boot; first one wins, second is a no-op. |
| Test DB | **SQLite in-memory.** Postgres-only features (JSONB ops, partial-index WHERE) are dialect-guarded; `_PK` variant downgrades `BIGINT PRIMARY KEY` → `INTEGER PRIMARY KEY` so SQLite autoincrement works. | Fast feedback (no Docker). Known trade-off: prod-only behavior isn't exercised. |
| Idempotency | All write helpers are idempotent on natural keys: `review_runs(repo, pr, head_sha, review_mode)`, `repositories.github_repository_id`, `pull_requests(repository_id, pr_number)`, `pending_reviews(repository_id, pr_number)`, `github_events.delivery_id`. | RQ retries don't create duplicates; webhook redeliveries are safe. |
| Code sharing | **`shared/reva/` extracted as an installable package** (`reva-shared`). Worker, api, and scheduler all `pip install ./shared`. | Single source for types/clients/db/formatters across processes. Imports: `from reva.X import ...` for shared, `from worker.X import ...` for worker-internal orchestration. |
| Scheduler topology | **Separate process / container** (planned, not yet built). Polls `pending_reviews` and enqueues into RQ. | Independent scaling and lifecycle from the api. |

## Contracts you can rely on

- `reva.types` is the single source of truth for the JSON shape. The Claude tool schema is derived from it via `reva.review_tool.build_review_tool_schema()`. Don't hand-write a second schema.
- `ClaudeClient.review()` signature is fixed: `(system_blocks, user_prompt, tools, tool_choice, model=None, max_tokens=8192) -> ClaudeResponse`.
- `Reviewer` collaborates with two **Protocols** (`GitHubReader`, `RepoLookup`) — implementations don't exist yet but the surface is locked.
- `ReviewResult.status ∈ {completed, stale, declined, failed}`. Stale and Declined are **outcomes, not exceptions** — `Reviewer.execute` returns them so the caller persists them like a normal result. Only `TransientError` / `PermanentError` propagate up.
- Exit conditions for `tasks.run_review`: TransientError → re-raised so RQ retries; PermanentError → persist `status=failed`, then re-raise so RQ marks the job failed.

## What is open

### Next slice candidates (pick one)

1. **Slice 9b — `api/` + `scheduler/`** — Now that `shared/` exists, build the FastAPI webhook receiver (writes events, upserts repo/pr/pending_review) and the separate scheduler container (polls pending_reviews, enqueues into RQ). Doc 04 + 05. After this, real PR pushes flow end-to-end through REVA. Plan in earlier message holds: ~30 new tests, two new containers, deferred internal `/api/v1/*` endpoints and comment triggers.

2. **`docker-compose.yml` + Nginx** — wire up Postgres + Redis + worker + api + scheduler + Nginx (TLS). Doc 09. Can ship before 9b if you want to bring up Postgres/Redis first.

3. **TUI** (`tui/`) — Bubble Tea. Doc 10. Depends on the api existing.

### Deferred work (tracked here so it doesn't get lost)

- **Per-hunk `skip_paths` filtering**: today the worker only declines a review if *every* changed file matches `skip_paths`. The intended end-state is to strip matching hunks from the unified diff before sending to Claude (e.g. drop `package-lock.json` chunks but still review the rest of the PR). Owner: future "diff filtering" sub-slice. Touchpoint: `worker/reviewer.py` step 9 — replace the all-match decline with `diff = filter_diff(diff, skip_patterns)` and re-run the size guards on the filtered diff.
- **`deep_review_paths` auto-elevation**: when a touched path matches a deep-review glob, the scheduler (not Reviewer) should flip `review_mode` to `"deep"`. Belongs in `api/services/scheduler.py`, not in this worker.
- **`min_severity_for_inline` / `min_confidence_for_inline`**: these are *output-time* filters applied by the GitHub poster when deciding which findings become inline comments. Reviewer intentionally keeps all findings.

### Doc cleanup still pending

ARIA placeholder still appears in these docs and should be replaced with REVA when each component is implemented:

```
doc/02-github-app-setup.md       2 hits
doc/03-database-schema.md        2 hits
doc/04-fastapi-service.md        1 hit
doc/06-review-worker.md          1 hit  (small reference in declined comment template)
doc/07-claude-prompts.md         2 hits (inside prompt content — KEEP if Claude needs to know its name)
doc/08-github-output.md          9 hits
doc/10-tui.md                    6 hits
doc/11-notifications-and-alerting.md  6 hits
doc/12-configuration.md          2 hits
doc/pr-review-requirements.md    8 hits
```

Note: ARIA inside `system.md`-quoted prompt content in `doc/07` and elsewhere is **prompt copy** — when you implement the actual prompts in `prompts/system.md`, write "REVA" there.

### Known doc gaps not yet fixed

- `doc/01-architecture.md` says "Automatic retry (3 attempts with backoff)" without specifying the layer. Once tasks.py retry config is wired, update this to point to RQ.
- `doc/05-queue-and-debounce.md` has not been updated to mention the `rq.Retry(max=3, interval=[30,120,300])` parameters — do this when implementing the scheduler.
- `doc/09-docker-and-deployment.md` not reviewed yet this session.
- `prompts/` directory does not exist yet on disk. `prompt_builder.py` expects `/app/prompts/` at runtime (configurable). When you create the directory, the file list must be: `system.md`, `diff_review.md`, `deep_review.md`, `odoo19.md`, `CHANGELOG.md`.

## Gotchas / non-obvious bits

- **Pricing in `cost.py` is a placeholder** based on the public Sonnet 4 / Opus 4 baseline. Verify against current Anthropic pricing before TUI cost metrics are trusted. Cache-read ≈ 10% of input; 5-min cache-write ≈ 1.25× input.
- **`anthropic-version: "2023-06-01"`** — prompt caching is GA on this version, no beta header required.
- **`Finding.title` is capped at 80 chars** in pydantic. Claude will get a 400 from the tool input if it produces a longer title — handle as `PermanentError` from the tool_use parser, not a silent truncation.
- **Token estimate is `len(diff) // 4`** — coarse but cheap. Don't replace with a tokenizer unless we see misclassification in production; the cost of being too conservative is just declining a PR.
- **`Reviewer` takes `RepoLookup` for owner/name** rather than touching SQLAlchemy. When implementing the DB layer, expose `RepoLookup` as a thin wrapper around session queries, NOT as a model method.
- **`prompt_builder._read` opens files at call time**, not at startup. Hot-reload of prompts works without restarting the worker. If we add a file watcher later, this design supports it.
- **No git repo yet.** When you run `git init`, do it at `/Users/joseph/Projects/cu_reva/` so `doc/` and `worker/` are both tracked.

## Verification commands

```bash
# 1. Ensure Python 3.14 is available
brew install python@3.14            # macOS; use distro package on Linux
/opt/homebrew/bin/python3.14 --version

# 2. Create / refresh venv
cd /Users/joseph/Projects/cu_reva/worker
/opt/homebrew/bin/python3.14 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements-dev.txt

# 3. Run the test suite
.venv/bin/python -m pytest tests/ -v
# Expected: 12 passed
```

## Pointers

- **Architecture & data flow**: `doc/01-architecture.md`
- **DB schema (authoritative)**: `doc/03-database-schema.md`
- **Worker lifecycle (21 steps)**: `doc/06-review-worker.md` § Worker Lifecycle per Job
- **Severity / category / output contract**: `doc/pr-review-requirements.md`
- **Prompt content**: `doc/07-claude-prompts.md`
- **What REVA must NOT do**: `doc/pr-review-requirements.md` §11
