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
    full_name TEXT NOT NULL,
    default_branch TEXT DEFAULT 'main',
    installation_id BIGINT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    config_cache JSONB,
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
    state TEXT NOT NULL,
    draft BOOLEAN NOT NULL DEFAULT FALSE,
    created_at_github TIMESTAMPTZ,
    updated_at_github TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(repository_id, pr_number)
);

-- Debounce buffer
CREATE TABLE pending_reviews (
    id BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL REFERENCES repositories(id),
    pull_request_id BIGINT NOT NULL REFERENCES pull_requests(id),
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    installation_id BIGINT NOT NULL,
    trigger_event TEXT NOT NULL,
    review_mode TEXT NOT NULL DEFAULT 'diff',
    scheduled_at TIMESTAMPTZ NOT NULL,
    consumed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(repository_id, pr_number)
);

-- One review attempt per PR commit
CREATE TABLE review_runs (
    id BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL REFERENCES repositories(id),
    pull_request_id BIGINT NOT NULL REFERENCES pull_requests(id),
    head_sha TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    trigger_event TEXT NOT NULL,
    review_mode TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    estimated_cost_usd NUMERIC(12, 6),
    risk_level TEXT,
    summary TEXT,
    finding_count INTEGER DEFAULT 0,
    decline_reason TEXT,
    check_run_id BIGINT,
    review_id BIGINT,
    error_message TEXT,
    error_class TEXT,
    worker_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(repository_id, pull_request_id, head_sha, review_mode)
);

-- Structured findings from Claude
CREATE TABLE review_findings (
    id BIGSERIAL PRIMARY KEY,
    review_run_id BIGINT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    file_path TEXT,
    line_start INTEGER,
    line_end INTEGER,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    suggestion TEXT,
    confidence NUMERIC(3, 2),
    is_odoo_specific BOOLEAN DEFAULT FALSE,
    github_comment_id BIGINT,
    posted_to_github BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Raw webhook deliveries for debugging / replay
CREATE TABLE github_events (
    id BIGSERIAL PRIMARY KEY,
    delivery_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    action TEXT,
    repository_full_name TEXT,
    sender_login TEXT,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed BOOLEAN NOT NULL DEFAULT FALSE,
    processed_at TIMESTAMPTZ
);

-- Job state tracking
CREATE TABLE review_jobs (
    id BIGSERIAL PRIMARY KEY,
    rq_job_id TEXT UNIQUE,
    repository_id BIGINT NOT NULL REFERENCES repositories(id),
    pull_request_id BIGINT NOT NULL REFERENCES pull_requests(id),
    head_sha TEXT NOT NULL,
    review_mode TEXT NOT NULL DEFAULT 'diff',
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    worker_id TEXT
);

-- Indexes
CREATE INDEX idx_repos_full_name ON repositories(full_name);
CREATE INDEX idx_prs_repo_number ON pull_requests(repository_id, pr_number);
CREATE INDEX idx_prs_author ON pull_requests(author_login);
CREATE INDEX idx_pending_reviews_scheduled ON pending_reviews(scheduled_at) WHERE consumed = FALSE;
CREATE INDEX idx_review_runs_repo_created ON review_runs(repository_id, created_at DESC);
CREATE INDEX idx_review_runs_status ON review_runs(status);
CREATE INDEX idx_review_runs_pr ON review_runs(pull_request_id, created_at DESC);
CREATE INDEX idx_findings_run ON review_findings(review_run_id);
CREATE INDEX idx_findings_severity ON review_findings(severity);
CREATE INDEX idx_findings_category ON review_findings(category);
CREATE INDEX idx_findings_file ON review_findings(file_path);
CREATE INDEX idx_events_received ON github_events(received_at DESC);
CREATE INDEX idx_events_repo ON github_events(repository_full_name, received_at DESC);
CREATE INDEX idx_jobs_status ON review_jobs(status);
