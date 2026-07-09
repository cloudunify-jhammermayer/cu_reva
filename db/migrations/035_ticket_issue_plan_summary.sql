-- Plain-English ticket summary the planner emits for the parent (epic) body,
-- so the epic always reads in English regardless of the ticket's language.
-- NULL on runs planned before this rollout. See reva.types.TicketIssuePlan.
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS plan_summary TEXT;
