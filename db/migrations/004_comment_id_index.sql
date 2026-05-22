-- Index for looking up findings by the GitHub comment ID REVA posted.
-- Used to match incoming reply webhooks back to the original finding.
CREATE INDEX IF NOT EXISTS idx_findings_github_comment_id
    ON review_findings(github_comment_id)
    WHERE github_comment_id IS NOT NULL;
