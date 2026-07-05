-- Timesheet wording review. Metadata only: description texts are never stored
-- at rest except callback_payload (updated_desc texts), kept only until the
-- Odoo callback succeeds and then cleared.

CREATE TABLE IF NOT EXISTS timesheet_review_runs (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT,
    odoo_instance_id BIGINT REFERENCES odoo_instances(id),
    request_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    total_lines INTEGER NOT NULL DEFAULT 0,
    ok_count INTEGER NOT NULL DEFAULT 0,
    rewritten_count INTEGER NOT NULL DEFAULT 0,
    needs_human_count INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd NUMERIC(12,6),
    callback_payload JSONB,
    callback_sent_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_timesheet_runs_pending
    ON timesheet_review_runs (odoo_instance_id, request_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_timesheet_runs_created
    ON timesheet_review_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_timesheet_runs_status
    ON timesheet_review_runs (status);

CREATE TABLE IF NOT EXISTS timesheet_review_lines (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES timesheet_review_runs(id) ON DELETE CASCADE,
    line_id BIGINT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_timesheet_lines_run_line
    ON timesheet_review_lines (run_id, line_id);
