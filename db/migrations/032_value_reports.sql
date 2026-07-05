-- Monthly value reports (spec 2026-07-05): generated always, Chat delivery
-- opt-in. One row per period; re-runs replace.
CREATE TABLE IF NOT EXISTS value_reports (
    id BIGSERIAL PRIMARY KEY,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    content_md TEXT NOT NULL,
    stats JSONB,
    chat_sent BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_value_reports_period
    ON value_reports (period_start, period_end);
