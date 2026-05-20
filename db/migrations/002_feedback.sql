-- Developer reactions to REVA's review comments
CREATE TABLE review_feedback (
    id BIGSERIAL PRIMARY KEY,
    review_finding_id BIGINT NOT NULL REFERENCES review_findings(id) ON DELETE CASCADE,
    review_run_id BIGINT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    github_comment_id BIGINT NOT NULL,
    reactor_login TEXT NOT NULL,
    reaction TEXT NOT NULL,
    is_positive BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(review_finding_id, reactor_login, reaction)
);

CREATE INDEX idx_feedback_finding ON review_feedback(review_finding_id);
CREATE INDEX idx_feedback_run ON review_feedback(review_run_id);
CREATE INDEX idx_feedback_positive ON review_feedback(is_positive);
