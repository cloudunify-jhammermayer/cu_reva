CREATE TABLE ticket_analyses (
    id                    BIGSERIAL PRIMARY KEY,
    job_id                TEXT,
    ticket_id             INTEGER NOT NULL,
    model_name            TEXT NOT NULL,
    field_name            TEXT NOT NULL,
    input_text            TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending',
    result_html           TEXT,
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

CREATE UNIQUE INDEX idx_ticket_analyses_job_id
    ON ticket_analyses (job_id)
    WHERE job_id IS NOT NULL;

CREATE INDEX idx_ticket_analyses_status    ON ticket_analyses (status);
CREATE INDEX idx_ticket_analyses_ticket_id ON ticket_analyses (ticket_id);
