-- Ticket<->PR loop closure (spec 2026-07-05): structured analyses feed
-- AC-grounded reviews; created issues carry an optional assignee; merged PRs
-- post change notes to Odoo (deduped per PR+ticket).
ALTER TABLE ticket_analyses ADD COLUMN IF NOT EXISTS result_structured JSONB;
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS github_username TEXT;

CREATE TABLE IF NOT EXISTS change_notes (
    id BIGSERIAL PRIMARY KEY,
    repo_full_name TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    ticket_id BIGINT NOT NULL,
    odoo_instance_id BIGINT NOT NULL REFERENCES odoo_instances(id),
    model_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    note_html TEXT,
    error_message TEXT,
    estimated_cost_usd NUMERIC(12, 6),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_change_notes_dedup
    ON change_notes (repo_full_name, pr_number, ticket_id);
CREATE INDEX IF NOT EXISTS idx_change_notes_created_at ON change_notes (created_at);
