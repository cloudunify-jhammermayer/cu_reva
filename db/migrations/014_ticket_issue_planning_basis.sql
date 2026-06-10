-- Don't keep the consultant DOCX on the server (space): the document flows
-- through the RQ job params transiently and is needed only at first-plan time.
-- A requeue resumes from the persisted plan, never from the doc.
ALTER TABLE ticket_issue_runs DROP COLUMN IF EXISTS description_docx;

-- Identifies WHAT a run planned from ("docx:<sha1[:16]>" or "text:<sha1[:16]>"),
-- computed once at row creation. Lets a re-run detect a revised spec (different
-- basis -> fresh issue set, no stale-plan adoption) without keeping the doc.
ALTER TABLE ticket_issue_runs ADD COLUMN planning_basis TEXT;
