-- Optional repo URL on ticket analyses (spec 2026-07-12): the Odoo record
-- knows its repo (reva_github_url) at analysis time — persist it so the TUI
-- Tickets tab can group analysis-only tickets under their repo instead of
-- "(no repo yet)". NULL for legacy/analysis-only rows. Mirrors
-- reva/db/models.py::TicketAnalysis.
ALTER TABLE ticket_analyses ADD COLUMN IF NOT EXISTS github_url TEXT;
