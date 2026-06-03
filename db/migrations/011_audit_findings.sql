-- Audit findings: one row per finding produced by a repo audit. Audits used to
-- discard everything but a count; now each finding is persisted, and major/
-- critical ones are also opened as GitHub issues (github_issue_number).
CREATE TABLE IF NOT EXISTS audit_findings (
    id BIGSERIAL PRIMARY KEY,
    audit_run_id BIGINT NOT NULL REFERENCES audit_runs(id) ON DELETE CASCADE,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    file_path TEXT,
    line_start INTEGER,
    line_end INTEGER,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    suggestion TEXT,
    confidence NUMERIC(3, 2),
    is_odoo_specific BOOLEAN NOT NULL DEFAULT FALSE,
    github_issue_number BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_findings_run ON audit_findings (audit_run_id);
CREATE INDEX idx_audit_findings_severity ON audit_findings (severity);
