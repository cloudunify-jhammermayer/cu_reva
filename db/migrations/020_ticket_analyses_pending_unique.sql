-- One in-flight (pending) analysis per (instance, ticket, model, field). Backs
-- the submit-time dedup with a race-proof constraint: two concurrent POSTs that
-- both pass the check-then-insert dedup now collide here (the loser catches
-- IntegrityError and returns the winner's id) instead of creating two rows and
-- two paid Claude jobs (M10). Mirrors idx_ticket_issue_runs_pending (018).
CREATE UNIQUE INDEX IF NOT EXISTS idx_ticket_analyses_pending
    ON ticket_analyses (odoo_instance_id, ticket_id, model_name, field_name)
    WHERE status = 'pending';
