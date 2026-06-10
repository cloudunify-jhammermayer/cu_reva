CREATE TABLE ticket_issue_runs (
    id                    BIGSERIAL PRIMARY KEY,
    job_id                TEXT,
    ticket_id             INTEGER NOT NULL,
    model_name            TEXT NOT NULL,
    github_url            TEXT NOT NULL,
    name                  TEXT NOT NULL,
    description           TEXT NOT NULL,
    analysis_html         TEXT NOT NULL,
    priority              TEXT NOT NULL,
    ticket_url            TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending',
    issues                JSONB,
    error_message         TEXT,
    model                 TEXT,
    input_tokens          INTEGER DEFAULT 0,
    output_tokens         INTEGER DEFAULT 0,
    cache_read_tokens     INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    estimated_cost_usd    NUMERIC(12, 6) DEFAULT 0,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at          TIMESTAMPTZ
);

CREATE UNIQUE INDEX idx_ticket_issue_runs_job_id
    ON ticket_issue_runs (job_id)
    WHERE job_id IS NOT NULL;

-- One in-flight run per Odoo record: closes the check-then-insert race where
-- two concurrent POSTs would both pass the pending dedup and double-create.
CREATE UNIQUE INDEX idx_ticket_issue_runs_pending
    ON ticket_issue_runs (ticket_id, model_name)
    WHERE status = 'pending';

CREATE INDEX idx_ticket_issue_runs_status     ON ticket_issue_runs (status);
CREATE INDEX idx_ticket_issue_runs_ticket_id  ON ticket_issue_runs (ticket_id);
-- The recurring retention purge filters on created_at (rows are scrubbed, not
-- deleted, so the table grows forever — without this it full-scans each pass).
CREATE INDEX idx_ticket_issue_runs_created_at ON ticket_issue_runs (created_at);
