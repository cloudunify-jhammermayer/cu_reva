-- The default /reviews feed (TUI Reviews tab, polled) orders by created_at DESC
-- with no repo/PR filter, but the only created_at indexes are composite
-- (repository_id|pull_request_id, created_at) — none serves a global recency
-- sort, so Postgres top-N-sorts the whole table each poll. A standalone index
-- serves both the ORDER BY and the paired count().
CREATE INDEX IF NOT EXISTS idx_review_runs_created ON review_runs (created_at DESC);
