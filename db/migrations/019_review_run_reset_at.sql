-- When an explicit re-review (a /review comment or manual requeue) reuses the
-- same review_runs row, reset_at marks when its posted-state was cleared. Crash
-- recovery (find_pr_review_id) scopes to reviews submitted at/after reset_at so
-- it recovers only the current attempt's review, never the stale pre-reset one
-- (which shares the same "Run #N" marker). NULL for runs never re-reviewed —
-- recovery falls back to created_at. See runner._post_result_to_github (H3/PR-9).
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS reset_at TIMESTAMPTZ;
