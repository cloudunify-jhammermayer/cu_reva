# 03 — Database Schema

## Overview

PostgreSQL 16 is the single source of truth. All review data, job state, metrics, and audit logs live here. Redis is used only as a transient job broker — never as a data store.

## Migration Strategy

Use plain SQL migration files in `db/migrations/`, applied in order. Each file is prefixed with a sequence number. The FastAPI app checks on startup whether migrations have been applied (via a `schema_migrations` table) and runs any pending ones.

For the MVP, this is simpler than Alembic and gives full control over the SQL.

## Schema: Migration 001 — Initial

```sql
-- db/migrations/001_initial.sql

-- Migration tracking
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Installed repositories
CREATE TABLE repositories (
    id BIGSERIAL PRIMARY KEY,
    github_repository_id BIGINT UNIQUE NOT NULL,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    full_name TEXT NOT NULL,                -- "org/repo"
    default_branch TEXT DEFAULT 'main',
    installation_id BIGINT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    config_cache JSONB,                     -- cached .claude-review.yml
    config_cached_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Pull request metadata
CREATE TABLE pull_requests (
    id BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL REFERENCES repositories(id),
    github_pr_id BIGINT NOT NULL,
    pr_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    author_login TEXT,
    base_branch TEXT NOT NULL,
    head_branch TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    state TEXT NOT NULL,                    -- open, closed, merged
    draft BOOLEAN NOT NULL DEFAULT FALSE,
    created_at_github TIMESTAMPTZ,
    updated_at_github TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(repository_id, pr_number)
);

-- Debounce buffer: pending reviews waiting for the 10-minute window
CREATE TABLE pending_reviews (
    id BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL REFERENCES repositories(id),
    pull_request_id BIGINT NOT NULL REFERENCES pull_requests(id),
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    installation_id BIGINT NOT NULL,
    trigger_event TEXT NOT NULL,            -- opened, synchronize, reopened, ready_for_review, manual
    review_mode TEXT NOT NULL DEFAULT 'diff', -- diff, deep
    scheduled_at TIMESTAMPTZ NOT NULL,      -- now() + 10 minutes
    consumed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(repository_id, pr_number)        -- only one pending review per PR
);

-- One review attempt per PR commit
CREATE TABLE review_runs (
    id BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL REFERENCES repositories(id),
    pull_request_id BIGINT NOT NULL REFERENCES pull_requests(id),
    head_sha TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued, running, completed, failed, stale, declined
    trigger_event TEXT NOT NULL,
    review_mode TEXT NOT NULL,               -- diff, deep
    model TEXT,                              -- claude-sonnet-4-20250514, etc.
    prompt_version TEXT,                     -- e.g. "v1.0", "v1.1"
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost_usd NUMERIC(12, 6),
    risk_level TEXT,                         -- low, medium, high, critical
    summary TEXT,
    finding_count INTEGER DEFAULT 0,
    decline_reason TEXT,                     -- set when status = declined (e.g. "diff too large")
    check_run_id BIGINT,                    -- GitHub Check Run ID
    review_id BIGINT,                       -- GitHub PR Review ID
    error_message TEXT,
    error_class TEXT,                        -- transient, permanent
    worker_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(repository_id, pull_request_id, head_sha, review_mode)
);

-- Structured findings from Claude
CREATE TABLE review_findings (
    id BIGSERIAL PRIMARY KEY,
    review_run_id BIGINT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    severity TEXT NOT NULL,                  -- info, minor, major, critical
    category TEXT NOT NULL,                  -- bug, security, performance, maintainability, etc.
    file_path TEXT,
    line_start INTEGER,
    line_end INTEGER,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    suggestion TEXT,                         -- optional suggested fix
    confidence NUMERIC(3, 2),               -- 0.00 to 1.00
    is_odoo_specific BOOLEAN DEFAULT FALSE, -- Odoo 19 specific finding
    github_comment_id BIGINT,
    posted_to_github BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Raw webhook deliveries for debugging / replay
CREATE TABLE github_events (
    id BIGSERIAL PRIMARY KEY,
    delivery_id TEXT UNIQUE NOT NULL,        -- X-GitHub-Delivery header
    event_type TEXT NOT NULL,                -- pull_request, issue_comment, etc.
    action TEXT,                             -- opened, synchronize, created, etc.
    repository_full_name TEXT,
    sender_login TEXT,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed BOOLEAN NOT NULL DEFAULT FALSE,
    processed_at TIMESTAMPTZ
);

-- Job state tracking (mirrors RQ job state for observability)
CREATE TABLE review_jobs (
    id BIGSERIAL PRIMARY KEY,
    rq_job_id TEXT UNIQUE,                  -- RQ job ID for correlation
    repository_id BIGINT NOT NULL REFERENCES repositories(id),
    pull_request_id BIGINT NOT NULL REFERENCES pull_requests(id),
    head_sha TEXT NOT NULL,
    review_mode TEXT NOT NULL DEFAULT 'diff',
    status TEXT NOT NULL DEFAULT 'queued',   -- queued, started, completed, failed, cancelled
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    worker_id TEXT
);

-- Indexes for TUI queries and common lookups
CREATE INDEX idx_repos_full_name ON repositories(full_name);
CREATE INDEX idx_prs_repo_number ON pull_requests(repository_id, pr_number);
CREATE INDEX idx_prs_author ON pull_requests(author_login);
CREATE INDEX idx_pending_reviews_scheduled ON pending_reviews(scheduled_at) WHERE consumed = FALSE;
CREATE INDEX idx_review_runs_repo_created ON review_runs(repository_id, created_at DESC);
CREATE INDEX idx_review_runs_status ON review_runs(status);
CREATE INDEX idx_review_runs_pr ON review_runs(pull_request_id, created_at DESC);
CREATE INDEX idx_review_runs_author ON review_runs(pull_request_id);
CREATE INDEX idx_findings_run ON review_findings(review_run_id);
CREATE INDEX idx_findings_severity ON review_findings(severity);
CREATE INDEX idx_findings_category ON review_findings(category);
CREATE INDEX idx_findings_file ON review_findings(file_path);
CREATE INDEX idx_events_received ON github_events(received_at DESC);
CREATE INDEX idx_events_repo ON github_events(repository_full_name, received_at DESC);
CREATE INDEX idx_jobs_status ON review_jobs(status);
```

## Schema: Migration 002 — Developer Feedback

```sql
-- db/migrations/002_feedback.sql

-- Track developer reactions to ARIA's review comments
CREATE TABLE review_feedback (
    id BIGSERIAL PRIMARY KEY,
    review_finding_id BIGINT NOT NULL REFERENCES review_findings(id) ON DELETE CASCADE,
    review_run_id BIGINT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    github_comment_id BIGINT NOT NULL,
    reactor_login TEXT NOT NULL,             -- GitHub user who reacted
    reaction TEXT NOT NULL,                  -- thumbs_up, thumbs_down, confused
    is_positive BOOLEAN NOT NULL,            -- thumbs_up = true, thumbs_down/confused = false
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(review_finding_id, reactor_login, reaction)
);

CREATE INDEX idx_feedback_finding ON review_feedback(review_finding_id);
CREATE INDEX idx_feedback_run ON review_feedback(review_run_id);
CREATE INDEX idx_feedback_positive ON review_feedback(is_positive);
```

## Schema: Migration 003 — Prompt Tracking

```sql
-- db/migrations/003_prompt_tracking.sql

-- Track prompt versions for quality analysis
CREATE TABLE prompt_versions (
    id BIGSERIAL PRIMARY KEY,
    version TEXT UNIQUE NOT NULL,            -- "v1.0", "v1.1", etc.
    system_prompt_hash TEXT NOT NULL,        -- SHA256 of system prompt content
    review_prompt_hash TEXT NOT NULL,        -- SHA256 of review prompt content
    description TEXT,                        -- changelog entry
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Key Design Decisions

**`pending_reviews` has a UNIQUE on `(repository_id, pr_number)`**: This means only one pending review per PR at a time. Every new push UPSERTs this row, resetting `head_sha`, `scheduled_at`, and `trigger_event`. The old pending review is overwritten. This is the debounce mechanism.

**`review_runs` has a UNIQUE on `(repository_id, pull_request_id, head_sha, review_mode)`**: This prevents duplicate reviews for the same commit. If a job is retried, it updates the existing row rather than creating a new one.

**`github_events` stores raw payloads**: This allows webhook replay for debugging. These rows can be pruned after 30 days via a cron job since you confirmed you don't need long-term raw storage.

**`review_jobs` mirrors RQ state**: RQ is transient. If Redis restarts, job state is lost. This table provides durable job tracking for the TUI and debugging.

## Useful Analytics Queries

These are the kinds of queries the TUI and internal API will run:

```sql
-- Reviews per developer per week
SELECT p.author_login, DATE_TRUNC('week', r.created_at) AS week,
       COUNT(*) AS reviews, AVG(r.finding_count) AS avg_findings
FROM review_runs r
JOIN pull_requests p ON r.pull_request_id = p.id
WHERE r.status = 'completed'
GROUP BY p.author_login, week
ORDER BY week DESC, reviews DESC;

-- Most common finding categories
SELECT f.category, f.severity, COUNT(*) AS count
FROM review_findings f
GROUP BY f.category, f.severity
ORDER BY count DESC;

-- Cost per repository per month
SELECT repo.full_name, DATE_TRUNC('month', r.created_at) AS month,
       SUM(r.estimated_cost_usd) AS total_cost,
       COUNT(*) AS review_count
FROM review_runs r
JOIN repositories repo ON r.repository_id = repo.id
WHERE r.status = 'completed'
GROUP BY repo.full_name, month
ORDER BY month DESC;

-- Developer improvement over time (finding count trend)
SELECT p.author_login,
       DATE_TRUNC('month', r.created_at) AS month,
       AVG(r.finding_count) AS avg_findings,
       AVG(CASE WHEN f.severity IN ('major','critical') THEN 1 ELSE 0 END) AS pct_severe
FROM review_runs r
JOIN pull_requests p ON r.pull_request_id = p.id
LEFT JOIN review_findings f ON f.review_run_id = r.id
WHERE r.status = 'completed'
GROUP BY p.author_login, month
ORDER BY p.author_login, month;

-- Feedback quality (are ARIA's findings helpful?)
SELECT f.category, f.severity,
       COUNT(CASE WHEN fb.is_positive THEN 1 END) AS thumbs_up,
       COUNT(CASE WHEN NOT fb.is_positive THEN 1 END) AS thumbs_down,
       ROUND(COUNT(CASE WHEN fb.is_positive THEN 1 END)::NUMERIC /
             NULLIF(COUNT(fb.id), 0), 2) AS approval_rate
FROM review_findings f
LEFT JOIN review_feedback fb ON fb.review_finding_id = f.id
GROUP BY f.category, f.severity
ORDER BY approval_rate ASC;

-- Success rate
SELECT status, COUNT(*), ROUND(COUNT(*)::NUMERIC / SUM(COUNT(*)) OVER (), 2) AS pct
FROM review_runs
GROUP BY status;
```

## Backup Strategy

Daily pg_dump via host cron job:

```bash
#!/bin/bash
# scripts/backup.sh
BACKUP_DIR="/var/backups/claude-reviewer"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/reviews_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

docker compose exec -T postgres pg_dump -U review reviews | gzip > "$BACKUP_FILE"

# Keep last 30 days
find "$BACKUP_DIR" -name "reviews_*.sql.gz" -mtime +30 -delete

# Rsync to backup server
rsync -az "$BACKUP_DIR/" backup-server:/backups/claude-reviewer/
```

Cron entry on the host:

```
0 3 * * * /opt/claude-reviewer/scripts/backup.sh >> /var/log/claude-reviewer-backup.log 2>&1
```

## Cleanup Job

Prune old `github_events` and completed `review_jobs` monthly:

```sql
-- Run as a scheduled job or cron
DELETE FROM github_events WHERE received_at < now() - INTERVAL '30 days';
DELETE FROM review_jobs WHERE status IN ('completed', 'cancelled') AND completed_at < now() - INTERVAL '30 days';
```
