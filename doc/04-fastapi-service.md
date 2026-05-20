# 04 — FastAPI Service

## Overview

The FastAPI service is a single container with two roles:

1. **Webhook receiver** (`/webhooks/github`) — public endpoint behind Nginx. Receives GitHub events, verifies signatures, stores events, manages the debounce buffer.
2. **Internal API** (`/api/v1/*`) — serves the TUI and future web dashboards. Read-only queries against PostgreSQL.

Both roles share the same database connection pool and configuration.

## Application Structure

```
api/
├── Dockerfile
├── requirements.txt
└── app/
    ├── main.py              # FastAPI app, lifespan, startup tasks
    ├── config.py             # Settings via pydantic-settings
    ├── database.py           # SQLAlchemy async engine + session
    ├── models/               # SQLAlchemy ORM models
    │   ├── __init__.py
    │   ├── repository.py
    │   ├── pull_request.py
    │   ├── review_run.py
    │   ├── review_finding.py
    │   ├── review_feedback.py
    │   ├── pending_review.py
    │   ├── review_job.py
    │   └── github_event.py
    ├── routes/
    │   ├── __init__.py
    │   ├── webhooks.py       # POST /webhooks/github
    │   ├── reviews.py        # GET /api/v1/reviews, /api/v1/reviews/{id}
    │   ├── repositories.py   # GET /api/v1/repositories
    │   ├── findings.py       # GET /api/v1/findings
    │   ├── metrics.py        # GET /api/v1/metrics/*
    │   └── health.py         # GET /health
    ├── services/
    │   ├── __init__.py
    │   ├── github_app.py     # JWT creation, installation token cache
    │   ├── webhook_handler.py # Event processing logic
    │   ├── scheduler.py      # Debounce scheduler (background task)
    │   └── notification.py   # Google Chat webhook calls
    └── schemas/
        ├── __init__.py
        ├── webhook.py        # Pydantic models for webhook payloads
        ├── review.py         # Response schemas for TUI API
        └── metrics.py        # Metrics response schemas
```

## Dependencies (requirements.txt)

```
fastapi>=0.111.0
uvicorn[standard]>=0.30.0
sqlalchemy[asyncio]>=2.0.30
asyncpg>=0.29.0
pydantic>=2.7.0
pydantic-settings>=2.3.0
redis>=5.0.0
rq>=1.16.0
httpx>=0.27.0
PyJWT>=2.8.0
cryptography>=42.0.0
structlog>=24.1.0
```

## Configuration (config.py)

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://review:review@postgres:5432/reviews"
    database_sync_url: str = "postgresql://review:review@postgres:5432/reviews"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # GitHub App
    github_app_id: int
    github_webhook_secret: str
    github_private_key_path: str = "/run/secrets/github_private_key"

    # Debounce
    debounce_seconds: int = 600  # 10 minutes
    scheduler_interval_seconds: int = 30

    # Review defaults
    default_review_mode: str = "diff"
    max_diff_lines: int = 1000

    # Notifications
    google_chat_webhook_url: str = ""

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    class Config:
        env_file = ".env"

settings = Settings()
```

## Webhook Endpoint (routes/webhooks.py)

This is the core entry point. It handles:

1. Signature verification
2. Deduplication via delivery ID
3. Event storage
4. Routing by event type and action
5. Upserting the debounce buffer (pending_reviews)

```python
from fastapi import APIRouter, Request, HTTPException, Header, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
import structlog

router = APIRouter()
logger = structlog.get_logger()

@router.post("/webhooks/github", status_code=202)
async def receive_webhook(
    request: Request,
    x_github_delivery: str = Header(...),
    x_hub_signature_256: str = Header(...),
    x_github_event: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    body = await request.body()

    # 1. Verify signature
    if not verify_webhook_signature(body, x_hub_signature_256, settings.github_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()

    # 2. Deduplicate
    existing = await db.execute(
        select(GithubEvent).where(GithubEvent.delivery_id == x_github_delivery)
    )
    if existing.scalar_one_or_none():
        logger.info("duplicate_webhook", delivery_id=x_github_delivery)
        return {"status": "duplicate"}

    # 3. Store raw event
    event = GithubEvent(
        delivery_id=x_github_delivery,
        event_type=x_github_event,
        action=payload.get("action"),
        repository_full_name=payload.get("repository", {}).get("full_name"),
        sender_login=payload.get("sender", {}).get("login"),
        payload=payload,
    )
    db.add(event)
    await db.flush()

    # 4. Route by event type
    if x_github_event == "pull_request":
        await handle_pull_request_event(db, payload, x_github_delivery)
    elif x_github_event == "issue_comment":
        await handle_issue_comment_event(db, payload)
    elif x_github_event == "pull_request_review_comment":
        await handle_review_comment_event(db, payload)

    event.processed = True
    event.processed_at = func.now()
    await db.commit()

    return {"status": "accepted"}
```

## Pull Request Event Handler

```python
REVIEWABLE_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}

async def handle_pull_request_event(db: AsyncSession, payload: dict, delivery_id: str):
    action = payload["action"]
    pr_data = payload["pull_request"]
    repo_data = payload["repository"]

    # Skip non-reviewable actions
    if action not in REVIEWABLE_ACTIONS:
        logger.info("skipping_action", action=action)
        return

    # Skip drafts
    if pr_data.get("draft", False):
        logger.info("skipping_draft", pr=pr_data["number"])
        return

    # Upsert repository
    repo = await upsert_repository(db, repo_data, payload["installation"]["id"])

    # Upsert pull request
    pr = await upsert_pull_request(db, repo, pr_data)

    # Upsert pending review (debounce)
    stmt = insert(PendingReview).values(
        repository_id=repo.id,
        pull_request_id=pr.id,
        pr_number=pr.pr_number,
        head_sha=pr_data["head"]["sha"],
        installation_id=payload["installation"]["id"],
        trigger_event=action,
        review_mode=settings.default_review_mode,
        scheduled_at=func.now() + text(f"INTERVAL '{settings.debounce_seconds} seconds'"),
        consumed=False,
    ).on_conflict_do_update(
        index_elements=["repository_id", "pr_number"],
        set_={
            "head_sha": pr_data["head"]["sha"],
            "trigger_event": action,
            "scheduled_at": func.now() + text(f"INTERVAL '{settings.debounce_seconds} seconds'"),
            "consumed": False,
            "updated_at": func.now(),
        },
    )
    await db.execute(stmt)
    logger.info("pending_review_upserted", repo=repo.full_name, pr=pr.pr_number,
                sha=pr_data["head"]["sha"][:8])
```

## Manual Trigger Handler (Issue Comments)

```python
import re

TRIGGER_PATTERN = re.compile(r"^/(review|deep-review)\s*$", re.IGNORECASE)

async def handle_issue_comment_event(db: AsyncSession, payload: dict):
    action = payload["action"]
    if action != "created":
        return

    comment_body = payload["comment"]["body"].strip()
    match = TRIGGER_PATTERN.match(comment_body)
    if not match:
        return

    # Check if this is a PR comment (issues don't have pull_request key)
    issue = payload["issue"]
    if "pull_request" not in issue:
        return

    command = match.group(1).lower()
    review_mode = "deep" if command == "deep-review" else "diff"

    repo_data = payload["repository"]
    repo = await get_repository_by_github_id(db, repo_data["id"])
    if not repo:
        return

    pr = await get_pull_request(db, repo.id, issue["number"])
    if not pr:
        return

    # For manual triggers, skip debounce — enqueue immediately
    # Upsert pending_review with scheduled_at = now()
    stmt = insert(PendingReview).values(
        repository_id=repo.id,
        pull_request_id=pr.id,
        pr_number=pr.pr_number,
        head_sha=pr.head_sha,
        installation_id=repo.installation_id,
        trigger_event="manual",
        review_mode=review_mode,
        scheduled_at=func.now(),  # immediate
        consumed=False,
    ).on_conflict_do_update(
        index_elements=["repository_id", "pr_number"],
        set_={
            "head_sha": pr.head_sha,
            "trigger_event": "manual",
            "review_mode": review_mode,
            "scheduled_at": func.now(),
            "consumed": False,
            "updated_at": func.now(),
        },
    )
    await db.execute(stmt)
    await db.commit()
    logger.info("manual_review_triggered", repo=repo.full_name, pr=pr.pr_number, mode=review_mode)
```

## Internal API Endpoints (for TUI)

### Reviews List

```
GET /api/v1/reviews?repo=org/repo&status=completed&limit=50&offset=0
```

Returns paginated review runs with basic PR info. Supports filtering by repo, status, author, date range.

### Review Detail

```
GET /api/v1/reviews/{review_run_id}
```

Returns full review run with findings array.

### Findings

```
GET /api/v1/findings?severity=major,critical&category=security&repo=org/repo
```

Cross-repo finding search with filters.

### Metrics

```
GET /api/v1/metrics/dashboard
```

Returns aggregated dashboard data: reviews last 24h, success rate, avg duration, finding counts, cost.

```
GET /api/v1/metrics/developers?period=month
```

Returns per-developer stats: review count, avg findings, improvement trend.

```
GET /api/v1/metrics/cost?period=month&repo=org/repo
```

Returns cost data by repo and time period.

```
GET /api/v1/metrics/feedback
```

Returns feedback quality metrics: approval rate by category and severity.

### Failures

```
GET /api/v1/failures?limit=20
```

Returns recent failed review runs with error details.

### Health

```
GET /health
```

Returns service health: DB connectivity, Redis connectivity, worker status.

## Startup and Lifespan

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await run_migrations()
    await start_scheduler()
    logger.info("api_started")

    yield

    # Shutdown
    await stop_scheduler()
    logger.info("api_stopped")

app = FastAPI(title="ARIA PR Reviewer API", lifespan=lifespan)
app.include_router(webhook_router)
app.include_router(api_router, prefix="/api/v1")
app.include_router(health_router)
```

## Logging

Use structlog with JSON output:

```python
import structlog

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
)
```

Every log line includes: timestamp, level, event name, and contextual fields (delivery_id, repo, pr_number, sha). Never log secrets.
