-- Change notes deliver on the ticket-ready transition, batched into ONE
-- tickets.change-summary callback (spec 2026-07-11). delivered_at stamps the
-- rows shipped in a batch so reopen->re-ready sends only the new PRs and RQ
-- retries stay idempotent. pr_title/pr_url are captured at generation time so
-- the batched summary (assembled later, from the DB) can render each PR ref
-- without a GitHub round-trip.
ALTER TABLE change_notes ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;
ALTER TABLE change_notes ADD COLUMN IF NOT EXISTS pr_title TEXT;
ALTER TABLE change_notes ADD COLUMN IF NOT EXISTS pr_url TEXT;
