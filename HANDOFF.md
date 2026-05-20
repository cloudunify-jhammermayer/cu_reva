# REVA — Implementation Handoff

Persistent context for agents picking up this project. Read this **before**
touching code or docs. Updated: 2026-05-20 (after slice 10 — Docker Compose + Nginx).

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
- **Slice 9a** — extracted reusable building blocks into `reva/` as an installable package (`reva-shared`, `pyproject.toml` at project root). Moved 11 modules + the `db/` subpackage. Rewrote all imports (`from worker.X` → `from reva.X` for moved modules). `worker/conftest.py` adds both `worker/` and project root to `sys.path`; `worker/requirements-dev.txt` adds `-e ..`. Updated `worker/Dockerfile` to install the reva package first. **All 116 existing tests still pass** — refactor was lossless.
- **Slice 9b** — `api/` (FastAPI webhook receiver) + `scheduler/` (RQ enqueuer) as two new containers. **138 tests total.**
- **Slice 10** — Docker Compose + Nginx. `docker-compose.yml` (dev, direct port exposure), `docker-compose.prod.yml` (production, Nginx + certbot TLS, Docker secrets for PEM). Nginx config uses template substitution for `${REVA_DOMAIN}`. `Makefile` covers dev/prod/deploy/test/scale. `scripts/deploy.sh` and `scripts/setup-letsencrypt.sh`. `.env.example` and `.gitignore`. `secrets/` directory gitignored but kept in tree. Both compose files pass `docker compose config` validation. API: HMAC-SHA256 signature verification, upserts repo/PR/pending_review on reviewable `pull_request` events, stores all events in `github_events`, debounce upsert resets `scheduled_at` on synchronize, draft PRs skipped (except `ready_for_review`). Scheduler: polls `pending_reviews` where `consumed=False AND scheduled_at <= now()`, marks consumed first (crash-safe), checks idempotency against `review_runs`, enqueues `worker.tasks.run_review` with `rq.Retry(max=3, interval=[30,120,300])`. Each service has its own `Dockerfile`, `requirements.txt`, and `requirements-dev.txt`. **138 tests total: 116 worker + 12 api + 10 scheduler.**

### Files created (current layout after slice 9b)

```
reva/                                ✅ installable library (pyproject.toml at project root)
├── __init__.py                 version stub
├── types.py                    ✅ Finding, ReviewResult, RepoConfig, JobParams, ClaudeResponse, ContentBlock
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
    ├── repo_lookup.py          ✅ get_owner_name + get_pr_basic + DatabaseRepoLookup adapter
    └── writers.py              ✅ idempotent record_review_* + upserts + record_github_event + is_already_posted

worker/                              ✅ RQ worker — orchestration glue only
├── Dockerfile                      installs reva from project root first, then worker requirements
├── requirements.txt                worker-only: rq, redis
├── requirements-dev.txt            adds -e .. (project root) and pytest
├── README.md
└── worker/
    ├── __init__.py
    ├── reviewer.py                 ✅ pure Reviewer + GitHubReader/RepoLookup Protocols
    ├── runner.py                   ✅ WorkerContext + build_worker_context + run_review
    ├── tasks.py                    ✅ stable enqueue path (re-exports run_review)
    ├── settings.py                 ✅ frozen Settings dataclass; from_env classmethod
    └── main.py                     ✅ load Settings → build context → start RQ worker

api/                                 ✅ FastAPI webhook receiver
├── Dockerfile                      installs reva from root, then api requirements
├── requirements.txt                fastapi, uvicorn[standard]
├── requirements-dev.txt            adds -e .. and pytest + httpx
├── app/
│   ├── __init__.py
│   ├── main.py                     ✅ lifespan: migrate + set app.state.db/settings
│   ├── settings.py                 ✅ frozen Settings dataclass; from_env classmethod
│   ├── security.py                 ✅ verify_signature (HMAC-SHA256, constant-time)
│   ├── dependencies.py             ✅ get_db / get_settings FastAPI DI helpers
│   └── routes/
│       ├── webhooks.py             ✅ POST /webhooks/github — event store + debounce upsert
│       └── health.py               ✅ GET /health — SELECT 1 liveness
└── tests/
    ├── __init__.py
    ├── conftest.py                 ✅ adds api/ + project root to sys.path
    └── test_webhooks.py            ✅ 12 tests; TestClient + StaticPool SQLite in-memory

scheduler/                          ✅ Standalone poller — enqueues into RQ
├── Dockerfile                      installs reva from root, then scheduler requirements
├── requirements.txt                rq, redis
├── requirements-dev.txt            adds -e .. and pytest
├── scheduler/
│   ├── __init__.py
│   ├── main.py                     ✅ SIGTERM/SIGINT-safe loop; configurable poll_interval_seconds
│   ├── poller.py                   ✅ Poller.poll() — fetch due rows, mark consumed, idempotency check, enqueue
│   └── settings.py                 ✅ frozen Settings dataclass; from_env classmethod
└── tests/
    ├── __init__.py
    ├── conftest.py                 ✅ adds scheduler/ + project root to sys.path
    └── test_poller.py              ✅ 10 tests; FakeQueue + SQLite in-memory

nginx/                               ✅ Nginx reverse proxy (production)
├── Dockerfile                      nginx:1.27-alpine + template substitution
├── nginx.conf                      JSON access log, 10m client_max_body_size
└── templates/
    └── reva.conf.template          rate-limited webhook + /api/ + /health proxies;
                                    ${REVA_DOMAIN} substituted at container start

docker-compose.yml                   ✅ Dev compose (direct ports, bind-mount PEM)
docker-compose.prod.yml              ✅ Prod compose (Nginx, certbot, Docker secrets)
.env.example                         Required env vars with comments
.gitignore                           Ignores .env, secrets/, .venv/, __pycache__
Makefile                             dev / prod / deploy / logs / test / scale targets
scripts/
├── deploy.sh                        git pull → build → stop → up -d → health poll
└── setup-letsencrypt.sh             certbot standalone; run once before first deploy
secrets/
└── .gitkeep                         directory tracked; *.pem files gitignored

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
| Code sharing | **`reva/` at project root as an installable package** (`reva-shared`). Worker, api, and scheduler all `pip install .` from the root. | Single source for types/clients/db/formatters across processes. Imports: `from reva.X import ...` for shared, `from worker.X import ...` for worker-internal orchestration. |
| Scheduler topology | **Separate container**. Polls `pending_reviews` and enqueues into RQ. | Independent scaling and lifecycle from the api; both can restart without affecting the other. |
| Webhook raw body | FastAPI `await request.body()` called **before** JSON parse; stored bytes passed to HMAC verifier. | Standard GitHub webhook security pattern — signature covers the exact bytes GitHub sent. |
| Scheduler consume-first | Poller marks `consumed=True` **before** enqueuing into RQ. | If the process crashes after marking but before enqueuing, the row is lost — acceptable. The alternative (enqueue-then-mark) risks double-enqueue which is harder to recover from. Idempotency in `record_review_started` is the second line of defence. |
| Test DB (api) | `StaticPool` + `check_same_thread=False` for SQLite in-memory in api tests. | FastAPI `TestClient` runs sync handlers in a threadpool; without `StaticPool` each thread gets a fresh empty in-memory DB, missing all tables created in the fixture. |
| Build context | **Project root** for all three service Dockerfiles. | All Dockerfiles do `COPY pyproject.toml . && COPY reva/ ./reva/` so they need to see the root-level installable package. Compose: `context: . / dockerfile: api/Dockerfile`. |
| Nginx domain config | **Template substitution** (`nginx/templates/reva.conf.template`). `${REVA_DOMAIN}` substituted by the nginx entrypoint via `NGINX_ENVSUBST_FILTER=REVA_DOMAIN`. | Keeps the nginx image stateless (no rebuild needed for domain change). Nginx variables like `$host` are left untouched because the filter is scoped to `REVA_DOMAIN`. |
| Private key delivery | **Bind-mount** (`./secrets/github-app-private-key.pem`) in dev; **Docker secrets** (`/run/secrets/github_private_key`) in prod. Both cases read via `GITHUB_PRIVATE_KEY_PATH`. | Docker secrets are the correct prod pattern — file is injected via tmpfs, not exposed in `docker inspect` env. Dev uses a bind-mount for convenience. |

## Contracts you can rely on

- `reva.types` is the single source of truth for the JSON shape. The Claude tool schema is derived from it via `reva.review_tool.build_review_tool_schema()`. Don't hand-write a second schema.
- `ClaudeClient.review()` signature is fixed: `(system_blocks, user_prompt, tools, tool_choice, model=None, max_tokens=8192) -> ClaudeResponse`.
- `Reviewer` collaborates with two **Protocols** (`GitHubReader`, `RepoLookup`) — implementations don't exist yet but the surface is locked.
- `ReviewResult.status ∈ {completed, stale, declined, failed}`. Stale and Declined are **outcomes, not exceptions** — `Reviewer.execute` returns them so the caller persists them like a normal result. Only `TransientError` / `PermanentError` propagate up.
- Exit conditions for `tasks.run_review`: TransientError → re-raised so RQ retries; PermanentError → persist `status=failed`, then re-raise so RQ marks the job failed.

## What is open

### Next slice candidates (pick one)

1. **Internal API** (`api/routes/v1/`) — `/api/v1/repos`, `/api/v1/reviews`, `/api/v1/findings`, etc. Needed by the TUI. Deferred from slice 9b. Doc 04.

2. **TUI** (`tui/`) — Bubble Tea dashboard. Doc 10. Depends on the internal API existing.

3. **Google Chat notifications** — `runner.py` already has a placeholder. Add `GOOGLE_CHAT_WEBHOOK_URL` to worker Settings and post a summary card after review completion.

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
- **Git repo is initialized** at the project root. All of `doc/`, `reva/`, `worker/`, `prompts/`, and `db/` are tracked under the same repo.

## Verification commands

```bash
# 1. Ensure Python 3.14 is available
brew install python@3.14            # macOS; use distro package on Linux
python3.14 --version                # Linux: install via distro or pyenv

# 2. Worker tests (116)
cd worker && python3.14 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v

# 3. API tests (12)
cd ../api && python3.14 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v

# 4. Scheduler tests (10)
cd ../scheduler && python3.14 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v

# Expected totals: 116 + 12 + 10 = 138 passed
```

## Pointers

- **Architecture & data flow**: `doc/01-architecture.md`
- **DB schema (authoritative)**: `doc/03-database-schema.md`
- **Worker lifecycle (21 steps)**: `doc/06-review-worker.md` § Worker Lifecycle per Job
- **Severity / category / output contract**: `doc/pr-review-requirements.md`
- **Prompt content**: `doc/07-claude-prompts.md`
- **What REVA must NOT do**: `doc/pr-review-requirements.md` §11
