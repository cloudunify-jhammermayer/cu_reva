-- Audit log of privileged /api/v1 admin actions (requeue, manual review,
-- trigger audit, weekly report) — who/what/when, for forensics.
CREATE TABLE admin_audit (
    id BIGSERIAL PRIMARY KEY,
    action TEXT NOT NULL,
    target TEXT,
    actor TEXT,
    detail JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_admin_audit_created ON admin_audit(created_at DESC);
