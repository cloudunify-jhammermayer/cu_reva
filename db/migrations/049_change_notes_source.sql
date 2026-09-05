-- Where a change note's text came from (spec 2026-09-04-release-log-change-notes):
-- 'claude' (drafted from the PR diff, today's rows) or 'release-log' (the ticket's
-- entry in the repo's docs/releases/<name>.md; note_html stays empty because the
-- entry is re-read at delivery time and sent once per ticket).
-- Mirrors reva/db/models.py::ChangeNote.source.
ALTER TABLE change_notes ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'claude';
