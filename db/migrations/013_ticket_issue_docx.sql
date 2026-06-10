-- Consultant DOCX forwarded by Odoo (Contract 1, description_docx): persisted
-- so a requeue resumes with the same planning basis. Customer content — the
-- retention purge nulls it together with description/analysis_html.
ALTER TABLE ticket_issue_runs ADD COLUMN description_docx JSONB;
