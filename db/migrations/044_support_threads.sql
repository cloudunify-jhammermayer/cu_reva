-- Support-answer threads and turns (spec 2026-07-25). REVA owns the thread of
-- its own Q&A turns and replays them into the prompt; Odoo re-sends the full
-- chatter snapshot each turn (delta bookkeeping drifts the moment a message is
-- edited, deleted or backdated, with no cheap way to detect it).
--
-- Thread key includes field_name, matching idx_ticket_analyses_pending: two
-- delivery targets on one record must be able to coexist, and the inbound
-- dedup is per (instance, ticket, model, field).
--
-- odoo_instance_id is denormalised onto support_turns so
-- writers.sum_instance_cost_since (the per-instance budget gate) can sum it
-- alongside the other run tables without a join. Mirrors reva/db/models.py
-- (SupportThread, SupportTurn).

CREATE TABLE IF NOT EXISTS support_threads (
    id BIGSERIAL PRIMARY KEY,
    odoo_instance_id BIGINT REFERENCES odoo_instances(id),
    ticket_id INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    field_name TEXT NOT NULL,
    github_url TEXT,
    persona_snapshot JSONB,                -- what actually applied, for audit
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_turn_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_support_threads_record
    ON support_threads (odoo_instance_id, ticket_id, model_name, field_name);

CREATE TABLE IF NOT EXISTS support_turns (
    id BIGSERIAL PRIMARY KEY,
    thread_id BIGINT NOT NULL REFERENCES support_threads(id),
    odoo_instance_id BIGINT REFERENCES odoo_instances(id),
    seq INTEGER NOT NULL,
    job_id TEXT,
    question TEXT NOT NULL,
    answer_html TEXT,
    result_structured JSONB,
    request_kind TEXT,
    answer_status TEXT,
    grounding_level TEXT,                  -- 'none' | 'docs' | 'code'
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd NUMERIC(12, 6),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    callback_sent_at TIMESTAMPTZ,
    callback_error TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_support_turns_seq
    ON support_turns (thread_id, seq);

-- One pending turn per thread: backs the submit dedup against a concurrent
-- POST race, same role idx_ticket_analyses_pending plays for analyses.
CREATE UNIQUE INDEX IF NOT EXISTS uq_support_turns_pending
    ON support_turns (thread_id) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_support_turns_created_at ON support_turns (created_at);
