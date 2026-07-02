-- Normalized "owner/repo" (lowercased) for ticket_issue_runs so the issue
-- state-sync webhook can equality-match on an index instead of a leading-
-- wildcard `github_url ILIKE '%…%'` that full-scans the table and materializes
-- every historical run's text columns (M15).
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS repo_full_name TEXT;

-- Backfill: every stored github_url was validated by parse_github_repo_url at
-- submit time (the endpoint 422s otherwise), so it is always
-- https://github.com/<owner>/<repo> with an optional .git/trailing slash —
-- strip the prefix/suffix and lowercase to reproduce what the app now writes.
UPDATE ticket_issue_runs
SET repo_full_name = lower(
    regexp_replace(
        regexp_replace(btrim(github_url), '^https://github\.com/', ''),
        '(\.git)?/?$', ''
    )
)
WHERE repo_full_name IS NULL;

CREATE INDEX IF NOT EXISTS idx_ticket_issue_runs_repo_full_name
    ON ticket_issue_runs (repo_full_name);
