-- Audit runs: one row per triggered repo audit.
CREATE TABLE IF NOT EXISTS audit_runs (
    id BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL REFERENCES repositories(id),
    status TEXT NOT NULL DEFAULT 'started',
    requested_by TEXT,
    summary TEXT,
    model TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,
    finding_count INTEGER DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_runs_repository_id ON audit_runs (repository_id);
CREATE INDEX idx_audit_runs_status ON audit_runs (status);
