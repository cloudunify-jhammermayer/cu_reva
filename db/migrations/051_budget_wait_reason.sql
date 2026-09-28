-- Which budget a wait-and-resume job is waiting for (spec 2026-09-27-provider-
-- credit-wait). 'cap' = a full REVA spend cap; 'provider_credit' = the
-- Anthropic account balance is empty (treated like a full cap, spec
-- 2026-09-27-budget-wait-and-resume). NULL when not waiting.
-- Mirrors reva/db/models.py::{ReviewRun,TicketAnalysis,TicketIssueRun,
-- TimesheetReviewRun,SupportTurn}.budget_wait_reason.
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS budget_wait_reason TEXT;
ALTER TABLE ticket_analyses ADD COLUMN IF NOT EXISTS budget_wait_reason TEXT;
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS budget_wait_reason TEXT;
ALTER TABLE timesheet_review_runs ADD COLUMN IF NOT EXISTS budget_wait_reason TEXT;
ALTER TABLE support_turns ADD COLUMN IF NOT EXISTS budget_wait_reason TEXT;
