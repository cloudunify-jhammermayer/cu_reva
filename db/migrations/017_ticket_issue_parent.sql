-- The parent ("epic") issue that groups a ticket's generated issues as GitHub
-- sub-issues. NULL for legacy and single-issue runs. JSON: {number, id, url,
-- title, state}. Deliberately never sent to Odoo (the callback contract is
-- frozen); it exists only to wire up GitHub sub-issue links.
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS parent_issue JSONB;
