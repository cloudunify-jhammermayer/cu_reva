-- Per-finding outcome ledger (Tier 1): persist the implicit ground truth REVA
-- already computes in the delta-resolution loop, for quality analytics.
-- 'open' (default) -> 'resolved_by_fix' (the verifier confirmed a fix on a
-- later push) or 'still_open_at_merge' (the PR merged with the finding never
-- observed fixed). The partial index serves the future quality dashboard.
ALTER TABLE review_findings ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT 'open';
ALTER TABLE review_findings ADD COLUMN IF NOT EXISTS outcome_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_findings_outcome ON review_findings(outcome) WHERE outcome <> 'open';
