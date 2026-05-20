# 01 — System Architecture

## High-Level Data Flow

```
Developer pushes to PR
        │
        ▼
GitHub sends webhook (pull_request event)
        │
        ▼
┌───────────────────┐
│   Nginx (TLS)     │  ← Let's Encrypt cert, rate limiting
└───────┬───────────┘
        │
        ▼
┌───────────────────┐
│  FastAPI Service   │  ← Verify signature, validate, store event
│  (api container)   │     Upsert pending_review record in Postgres
└───────┬───────────┘     with scheduled_at = now() + 10 min
        │
        ▼
┌───────────────────┐
│   PostgreSQL       │  ← pending_reviews table (debounce buffer)
│  (postgres cont.)  │
└───────┬───────────┘
        │
        │  Scheduler loop (every 30s inside api container)
        │  picks up pending_reviews where scheduled_at <= now()
        │
        ▼
┌───────────────────┐
│   Redis + RQ       │  ← Job queued with repo_id, pr_number, head_sha
│  (redis container)  │
└───────┬───────────┘
        │
        ▼
┌───────────────────┐
│  Review Worker     │  ← Claims job from RQ
│  (worker cont.)    │     1. Create GitHub installation token
│                    │     2. Fetch PR metadata + diff
│                    │     3. Load .claude-review.yml + CLAUDE.md
│                    │     4. Check diff size (>1000 lines → decline)
│                    │     5. Call Claude Messages API
│                    │     6. Parse structured JSON response
│                    │     7. Store results in Postgres
│                    │     8. Post Check Run + PR Review to GitHub
│                    │     9. Send Google Chat alert if needed
└───────────────────┘

        │
        ▼
┌───────────────────┐
│  FastAPI Internal   │  ← /api/v1/reviews, /api/v1/metrics, etc.
│  API (same cont.)   │
└───────┬───────────┘
        │
        ▼
┌───────────────────┐
│  Go TUI            │  ← Connects to FastAPI internal API
│  (local binary)    │     Dashboard, reviews, findings, failures, metrics
└───────────────────┘
```

## Container Layout

Four Docker containers orchestrated via Docker Compose:

### Container 1: `nginx`

- Nginx reverse proxy with Let's Encrypt TLS
- Terminates HTTPS, forwards to FastAPI on port 8080
- Rate limiting on webhook endpoint
- Serves as the only public-facing container
- Exposes ports 80 (redirect to 443) and 443

### Container 2: `api`

- FastAPI application (uvicorn)
- Two responsibilities in one process:
  - **Webhook receiver**: `/webhooks/github` — verifies signatures, stores events, upserts pending reviews
  - **Internal API**: `/api/v1/*` — serves TUI and future dashboards
- Runs the debounce scheduler as a background task (asyncio)
- Connects to PostgreSQL and Redis
- Internal port 8080, not exposed to host

### Container 3: `worker`

- RQ worker process (can scale to N workers via `docker compose up --scale worker=N`)
- Consumes review jobs from Redis queue
- Calls Claude Messages API
- Calls GitHub API (installation tokens, PR data, posting reviews)
- Writes results to PostgreSQL
- Sends Google Chat notifications
- No public network exposure
- Resource-limited (CPU, memory, timeout)

### Container 4: `postgres`

- PostgreSQL 16
- Persistent volume for data
- Internal network only
- Daily pg_dump backup via host cron job to backup server

### Container 5: `redis`

- Redis 7
- Used only as RQ job broker
- No persistence needed (jobs are tracked in Postgres)
- Internal network only

## Network Topology

```
Internet
    │
    ▼
┌─────────┐     ┌──────────┐
│  Nginx   │────▶│  FastAPI  │
│ :443/:80 │     │  :8080   │
└─────────┘     └────┬─────┘
                     │
              ┌──────┼──────┐
              ▼      ▼      ▼
          ┌──────┐┌──────┐┌──────┐
          │Redis ││Postgres││Worker│
          │:6379 ││:5432  ││      │
          └──────┘└──────┘└──────┘
```

All containers share a single Docker bridge network (`reviewer-net`). Only Nginx binds to host ports. PostgreSQL, Redis, FastAPI, and Worker communicate internally.

The Worker also needs outbound HTTPS access to:
- `api.anthropic.com` (Claude API)
- `api.github.com` (GitHub API)
- `chat.googleapis.com` (Google Chat webhook)

## Data Ownership

| Data | Written by | Read by |
|---|---|---|
| `github_events` | API (webhook handler) | TUI (via API), debug |
| `pending_reviews` | API (webhook handler) | API (scheduler) |
| `repositories` | API (webhook handler) | Worker, TUI |
| `pull_requests` | API (webhook handler) | Worker, TUI |
| `review_runs` | Worker | TUI |
| `review_findings` | Worker | TUI |
| `review_feedback` | API (reaction webhook) | TUI |
| `review_jobs` | API (scheduler → RQ) | Worker |
| Redis queues | API (scheduler) | Worker |

## Scaling Considerations

For the current scale (5 developers, 1–2 PRs every 3 hours), a single instance of everything is more than sufficient. The architecture supports horizontal scaling if needed later:

- **Workers**: `docker compose up --scale worker=3` — RQ handles job distribution automatically.
- **API**: Can be replicated behind Nginx if webhook volume grows. Unlikely to be needed below 100 repos.
- **PostgreSQL**: Single instance is fine for thousands of reviews. Add read replicas only if TUI queries cause contention.
- **Redis**: Single instance handles thousands of jobs per second. Not the bottleneck.

## Failure Modes

| Failure | Impact | Recovery |
|---|---|---|
| API container down | Webhooks fail (GitHub retries) | Restart container; GitHub redelivers |
| Worker container down | Jobs pile up in Redis | Restart; worker resumes from queue |
| Redis down | New jobs can't be queued | Restart; pending_reviews in Postgres are re-evaluated by scheduler |
| Postgres down | Everything stalls | Restart; restore from backup if corrupt |
| Claude API down | Worker retries, then marks job failed | Automatic retry (3 attempts with backoff) |
| GitHub API down | Worker can't post review | Retry; results are in Postgres regardless |
| Nginx down | All external traffic blocked | Restart; GitHub retries webhooks |
