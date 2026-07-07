-- Ticket analysis v2 (spec 2026-07-07): delivery visibility. The Odoo
-- write_field callback runs AFTER the row flips to 'completed', so a failed
-- callback left a green "completed" for an analysis the consultant never
-- received. Track delivery explicitly. Mirrors reva/db/models.py::TicketAnalysis.
ALTER TABLE ticket_analyses ADD COLUMN IF NOT EXISTS callback_sent_at TIMESTAMPTZ;
ALTER TABLE ticket_analyses ADD COLUMN IF NOT EXISTS callback_error TEXT;
