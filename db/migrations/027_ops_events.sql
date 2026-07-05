-- Persistent component-degradation log (ops-event spec §6): every
-- caught-and-degraded error (CodeGraph fallback, callback failure, git retry,
-- core-knowledge degradation) is recorded here so a quietly-degrading system
-- is visible in the TUI, not only in container logs. Append-only; purged by
-- the daily retention pass (REVA_OPS_EVENTS_RETENTION_DAYS, default 30).
-- Mirrors reva/db/models.py::OpsEvent.
CREATE TABLE IF NOT EXISTS ops_events (
    id BIGSERIAL PRIMARY KEY,
    component TEXT NOT NULL,
    severity TEXT NOT NULL,
    event TEXT NOT NULL,
    detail JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ops_events_created_at ON ops_events (created_at);
CREATE INDEX IF NOT EXISTS idx_ops_events_component ON ops_events (component);
