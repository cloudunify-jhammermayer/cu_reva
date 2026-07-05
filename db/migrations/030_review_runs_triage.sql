-- Triage pre-pass stamp (triage-escalation spec): target depth chosen by the
-- escalate-only router ("full"/"deep"), NULL when not escalated or disabled.
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS triage_escalation TEXT;
