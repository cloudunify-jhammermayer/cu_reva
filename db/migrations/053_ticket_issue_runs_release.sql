-- The Odoo release a create-issues request named (spec
-- 2026-09-30-change-summary-followups): kept on the run so a requeue, which
-- rebuilds its job params from this row, writes the **Release:** line on the
-- issues again. NULL = the request carried no release, or the row predates
-- this migration.
-- Mirrors reva/db/models.py::TicketIssueRun.release_id / .release_name.
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS release_id BIGINT;
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS release_name TEXT;
