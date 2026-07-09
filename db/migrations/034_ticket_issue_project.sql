-- Optional GitHub Projects v2 board + planned date sent by Odoo with a
-- create-issues request (spec 2026-07-09). NULL → no Projects interaction.
-- Per-item projection state (node_id, project_item_id) lives inside the
-- existing issues/parent_issue JSON, not in columns.
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS github_project_url TEXT;
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS plan_date DATE;
