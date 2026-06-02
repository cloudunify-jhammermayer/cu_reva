-- CONC-1: let two worker jobs for the same (repo, pr, head_sha, review_mode)
-- coordinate so only ONE runs the paid review. Stores the RQ job id of the
-- claimant; claim_review_run() locks the row (FOR UPDATE) and refuses the claim
-- when a DIFFERENT job already holds it in 'running'. A retry of the same job id
-- re-claims (so RQ retries still complete). NULL = unclaimed / legacy row.
ALTER TABLE review_runs ADD COLUMN claimed_by_job_id TEXT;
