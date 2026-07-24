-- Force-push-aware delta + cross-branch review reuse (spec 2026-07-24).
-- diff_hash: fingerprint of a full-scope review's filtered diff (NULL on delta/declined).
-- delta_base_sha: persisted review scope (NULL = full-scope; non-NULL = delta run).
-- carried_from_run_id: the review_runs.id a carried-forward run reused (NULL = a real review).
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS diff_hash           TEXT;
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS delta_base_sha      TEXT;
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS carried_from_run_id BIGINT;
CREATE INDEX IF NOT EXISTS ix_review_runs_diff_hash
    ON review_runs (repository_id, diff_hash) WHERE diff_hash IS NOT NULL;
