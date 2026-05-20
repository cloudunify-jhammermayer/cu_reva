# 00 — Project Overview

## Project Name

**Claude PR Reviewer** — Automatic pull request review platform powered by Claude API.

## Agent Identity

**REVA** — Review & Evaluation Agent.

REVA appears in PR review comments, Check Run summaries, and the TUI header. Docs 01–13 still reference `REVA` from an earlier draft — those are updated as each component is implemented; treat any remaining `REVA` reference as a synonym for REVA.

## Goal

Build an internal platform that:

1. Automatically reviews GitHub pull requests using the Claude Messages API.
2. Posts structured review feedback (Check Runs + PR Reviews with inline comments) back to GitHub.
3. Stores all review data in PostgreSQL for analytics and operational visibility.
4. Exposes review history, developer metrics, cost data, and failures through a Go TUI (Bubble Tea) backed by a FastAPI internal API.
5. Sends alerts to Google Chat on failures and critical findings.
6. Supports per-repo configuration via `.claude-review.yml`.
7. Respects a `CLAUDE.md` in each repo for project-specific review context.

## Confirmed Tech Stack

| Layer | Technology |
|---|---|
| Webhook + Internal API | Python / FastAPI |
| Queue | Redis + RQ |
| Review Engine | Claude Messages API (Sonnet 4 default, Opus 4 for deep review) |
| Database | PostgreSQL 16 |
| Reverse Proxy / TLS | Nginx + Let's Encrypt (Certbot) |
| TUI | Go / Bubble Tea |
| Notifications | Google Chat incoming webhook |
| Deployment | Docker Compose on Hetzner dedicated server |
| Secret Management | Docker secrets + `.env` file (chmod 600, gitignored) |
| Source Control | Single monorepo on GitHub |

## Confirmed Decisions

| Decision | Choice |
|---|---|
| GitHub integration | GitHub App (Team plan, github.com) |
| Target repos | 1 org, starting with 5 repos, growing per project |
| Draft PRs | Skip |
| Fork PRs | Skip |
| Debounce | 10-minute delayed queue — only review latest SHA |
| Max diff size | 1000 lines — decline larger PRs with a comment suggesting split |
| Manual trigger | `/review` and `/deep-review` PR comments |
| Per-repo config | `.claude-review.yml` in repo root |
| Repo instructions | `CLAUDE.md` in repo root |
| Check Run blocking | Yes — critical/major findings block merge |
| Comment threshold | All severities posted, with confidence level shown |
| Review body format | Structured template |
| Developer feedback | GitHub reactions (👍/👎) on REVA's comments, tracked in DB |
| Prompt storage | `prompts/` directory in monorepo, version stored in DB |
| TUI data access | Via FastAPI internal API (not direct DB) |
| Logging | Structured JSON, log rotation |
| Alerting | Google Chat webhook + TUI failures view |
| Backups | Daily pg_dump cron to backup server |
| Data retention | Basic data kept, raw webhooks/Claude responses not stored long-term |
| Language awareness | Not in MVP, possible future addition |

## Repository Structure

```
claude-pr-reviewer/
├── docker-compose.yml
├── docker-compose.prod.yml
├── .env.example
├── .env                         # gitignored, chmod 600
├── Makefile
├── README.md
├── nginx/
│   ├── Dockerfile
│   ├── nginx.conf
│   ├── conf.d/
│   │   └── reviewer.conf
│   └── certs/                   # gitignored, Let's Encrypt mount
├── api/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py
│       ├── config.py
│       ├── database.py
│       ├── models/
│       │   ├── __init__.py
│       │   ├── repository.py
│       │   ├── pull_request.py
│       │   ├── review_run.py
│       │   ├── review_finding.py
│       │   ├── review_job.py
│       │   ├── review_feedback.py
│       │   └── github_event.py
│       ├── routes/
│       │   ├── __init__.py
│       │   ├── webhooks.py
│       │   ├── reviews.py
│       │   ├── repositories.py
│       │   ├── metrics.py
│       │   └── health.py
│       ├── services/
│       │   ├── __init__.py
│       │   ├── github_app.py
│       │   ├── webhook_handler.py
│       │   ├── scheduler.py
│       │   └── notification.py
│       └── schemas/
│           ├── __init__.py
│           ├── webhook.py
│           ├── review.py
│           └── metrics.py
├── worker/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── worker/
│       ├── __init__.py
│       ├── main.py
│       ├── tasks.py
│       ├── reviewer.py
│       ├── claude_client.py
│       ├── github_client.py
│       ├── diff_parser.py
│       ├── finding_normalizer.py
│       └── config_loader.py
├── prompts/
│   ├── system.md
│   ├── diff_review.md
│   ├── deep_review.md
│   ├── odoo19.md
│   └── CHANGELOG.md
├── config/
│   ├── default.yml
│   └── logging.yml
├── tui/
│   ├── go.mod
│   ├── go.sum
│   ├── main.go
│   ├── cmd/
│   │   └── root.go
│   └── internal/
│       ├── api/
│       │   └── client.go
│       ├── ui/
│       │   ├── app.go
│       │   ├── dashboard.go
│       │   ├── reviews.go
│       │   ├── findings.go
│       │   ├── failures.go
│       │   ├── metrics.go
│       │   └── styles.go
│       └── models/
│           └── types.go
├── scripts/
│   ├── backup.sh
│   ├── deploy.sh
│   ├── setup-github-app.sh
│   └── init-db.sh
├── db/
│   └── migrations/
│       ├── 001_initial.sql
│       └── 002_feedback.sql
├── secrets/                     # gitignored entirely
│   └── github-app-private-key.pem
└── docs/
    ├── 00-overview.md
    ├── 01-architecture.md
    ├── 02-github-app-setup.md
    ├── 03-database-schema.md
    ├── 04-fastapi-service.md
    ├── 05-queue-and-debounce.md
    ├── 06-review-worker.md
    ├── 07-claude-prompts.md
    ├── 08-github-output.md
    ├── 09-docker-and-deployment.md
    ├── 10-tui.md
    ├── 11-notifications-and-alerting.md
    ├── 12-configuration.md
    ├── 13-security.md
    └── pr-review-requirements.md
```

## Implementation Phases

| Phase | Scope | Estimated Effort |
|---|---|---|
| 1 | GitHub App + webhook ingestion + DB schema | 2–3 days |
| 2 | Redis + RQ queue + debounce scheduler | 1–2 days |
| 3 | Worker skeleton + Claude integration + structured output | 3–4 days |
| 4 | GitHub output (Check Runs + PR Reviews + inline comments) | 2–3 days |
| 5 | FastAPI internal API for TUI | 1–2 days |
| 6 | Go TUI (Bubble Tea) | 3–4 days |
| 7 | Google Chat notifications | 0.5 day |
| 8 | Per-repo config + CLAUDE.md support | 1 day |
| 9 | Developer feedback tracking (reactions) | 1 day |
| 10 | Hardening, logging, backups, deploy scripts | 2–3 days |

Total estimated: ~17–23 working days for one developer.

## Document Index

| File | Contents |
|---|---|
| `00-overview.md` | This file — project summary and decisions |
| `01-architecture.md` | System architecture, data flow, container layout |
| `02-github-app-setup.md` | Step-by-step GitHub App creation and configuration |
| `03-database-schema.md` | PostgreSQL schema, migrations, indexes |
| `04-fastapi-service.md` | FastAPI webhook receiver + internal API |
| `05-queue-and-debounce.md` | Redis + RQ setup, debounce strategy |
| `06-review-worker.md` | Worker implementation, Claude API integration |
| `07-claude-prompts.md` | Prompt design, versioning, REVA personality |
| `08-github-output.md` | Check Runs, PR Reviews, inline comments |
| `09-docker-and-deployment.md` | Dockerfiles, Compose, Nginx, TLS, deploy |
| `10-tui.md` | Bubble Tea TUI design and views |
| `11-notifications-and-alerting.md` | Google Chat webhooks, alert rules |
| `12-configuration.md` | Per-repo config, global defaults, CLAUDE.md |
| `13-security.md` | Secrets, isolation, input validation, cost control |
| `pr-review-requirements.md` | Severity levels, categories, output contract, Odoo 19 rules |
