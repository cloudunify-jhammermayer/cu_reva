-- Per-ticket timesheet actuals, pushed by Odoo when a ticket is marked done
-- (estimate-calibration loop C1, deferred from spec 2026-07-10-issue-estimates
-- -to-odoo). Estimates stay on the Projects board by design — this table holds
-- only the Odoo-side actual. One row per (instance, ticket): a re-done ticket
-- re-sends its totals and the latest push wins (reported_at tracks it).
-- Mirrors reva/db/models.py::TicketActual.
CREATE TABLE IF NOT EXISTS ticket_actuals (
    id BIGSERIAL PRIMARY KEY,
    odoo_instance_id BIGINT NOT NULL REFERENCES odoo_instances(id),
    ticket_id INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    actual_hours NUMERIC(8,2) NOT NULL,
    timesheet_line_count INTEGER,
    reported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ticket_actuals_instance_ticket
    ON ticket_actuals (odoo_instance_id, ticket_id, model_name);
