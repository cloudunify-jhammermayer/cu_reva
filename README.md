# REVA — Review & Evaluation Agent

Automatic GitHub PR review platform powered by the Claude Messages API.

REVA listens for `pull_request` webhook events, waits 10 minutes for the
developer to settle on a head SHA (debounce), runs Claude against the diff
under a strict tool-use contract, and posts a Check Run + PR Review (with
inline comments) back to GitHub. All review data is persisted to Postgres
for analytics, cost tracking, and developer feedback loops.

## What's here

```
.
├── doc/                  Architecture, schemas, and decision docs (start with 00-overview.md)
├── prompts/              REVA's actual prompt content; versioned via CHANGELOG.md
├── db/migrations/        Plain-SQL migrations applied at worker/api startup
├── shared/               Installable `reva` library — types, errors, clients, formatters, db
├── worker/               RQ-based review worker — Reviewer + orchestration glue, depends on shared/
├── api/                  (Not built yet) FastAPI webhook receiver
├── scheduler/            (Not built yet) Debounce poller — enqueues due jobs into RQ
├── tui/                  (Not built yet) Bubble Tea ops dashboard
├── HANDOFF.md            Per-slice implementation status and decision log
└── README.md             This file
```

Each subdirectory has its own README explaining its role.

## Tech stack

| Layer | Technology |
|---|---|
| Webhook + Internal API | Python / FastAPI |
| Job queue | Redis + RQ |
| Review engine | Claude Messages API (Sonnet 4.6 default, Opus 4.7 for `/deep-review`) |
| Database | PostgreSQL 16 |
| Reverse proxy / TLS | Nginx + Let's Encrypt |
| TUI | Go / Bubble Tea |
| Notifications | Google Chat incoming webhook |
| Container runtime | Docker Compose |
| Python | **3.14** everywhere |

## Running the tests

Requires Python 3.14 (`brew install python@3.14` on macOS).

```bash
cd worker
/opt/homebrew/bin/python3.14 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/
# Expected: 116 passed
```

Tests use SQLite in-memory and httpx MockTransport; no Docker required.

## Status

The worker pipeline is wired end-to-end (Reviewer → ClaudeClient → GitHubClient
→ DB writers → poster), and the reusable building blocks have been extracted
into `shared/reva/` as an installable library. What's still missing for a
fully working production system: the `api/` service (webhook), the
`scheduler/` service (debounce poller), `docker-compose.yml`, Nginx config,
and the TUI. See `HANDOFF.md` for the running implementation log.

## Where to read next

- `doc/00-overview.md` — project goals and decisions
- `doc/01-architecture.md` — system architecture and data flow
- `HANDOFF.md` — what's built, what's open, and the decisions behind each slice
