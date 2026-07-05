-- The ticket-analyses list endpoint orders by created_at DESC
-- (api/app/queries/ticket_analyses.py); every other run table already has a
-- created_at index. Mirrors reva/db/models.py::TicketAnalysis.
CREATE INDEX IF NOT EXISTS idx_ticket_analyses_created_at
    ON ticket_analyses (created_at);
