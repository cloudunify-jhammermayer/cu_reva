-- Track when weekly reports were enqueued to prevent duplicate sends across restarts.
CREATE TABLE weekly_reports (
    id BIGSERIAL PRIMARY KEY,
    enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    period_days INT NOT NULL DEFAULT 7
);

CREATE INDEX idx_weekly_reports_enqueued ON weekly_reports(enqueued_at DESC);
