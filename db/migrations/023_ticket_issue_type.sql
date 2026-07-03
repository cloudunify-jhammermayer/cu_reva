-- Optional fixed work-item type for a create-issues request (Odoo wizard flow).
-- NULL = the planner picks a type per issue (analysis flow and pre-type rows).
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS issue_type TEXT;
