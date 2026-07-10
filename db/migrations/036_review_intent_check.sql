-- Per-linked-issue conformance verdicts from the reviewer (advisory only).
-- JSON list of {issue_number, verdict, note}; NULL = no verdicts this run
-- (no linked issues, delta review, repo opt-out, or model omitted them).
-- See reva.types.IntentIssueVerdict / specs/2026-07-10-issue-conformance-check.
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS intent_check JSONB;
