-- Unified spend ledger: one row per paid Claude call (review, audit, reply),
-- so the rolling daily budget cap counts every kind of spend, not just
-- review_runs (SECU-3/SECU-4). The ledger is the single accounting source read
-- by sum_estimated_cost_since; per-domain tables keep their own cost columns
-- for display only.
CREATE TABLE claude_spend (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL,                 -- 'review' | 'audit' | 'reply'
    cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The cap query filters on created_at >= now() - 24h, so index it.
CREATE INDEX idx_claude_spend_created ON claude_spend(created_at);
