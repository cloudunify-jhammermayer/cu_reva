-- Wait-and-resume for budget-gated jobs (spec 2026-09-27-budget-wait-and-resume).
-- Set on the first time a job defers itself for budget, cleared when the paid
-- work starts. Odoo-facing rows keep status 'pending' (dedup indexes + the
-- Odoo status contract are untouched); review_runs use status 'waiting_budget'.
-- Mirrors reva/db/models.py::{ReviewRun,TicketAnalysis,TicketIssueRun,
-- TimesheetReviewRun,SupportTurn}.budget_wait_since.
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS budget_wait_since TIMESTAMPTZ;
ALTER TABLE ticket_analyses ADD COLUMN IF NOT EXISTS budget_wait_since TIMESTAMPTZ;
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS budget_wait_since TIMESTAMPTZ;
ALTER TABLE timesheet_review_runs ADD COLUMN IF NOT EXISTS budget_wait_since TIMESTAMPTZ;
ALTER TABLE support_turns ADD COLUMN IF NOT EXISTS budget_wait_since TIMESTAMPTZ;
