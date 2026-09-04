-- Release-log lookups requested by Odoo (spec 2026-09-04-release-log-requirements, R2).
-- One row per POST /api/v1/release-note; `id` is the note_id Odoo stores and
-- echoes on the callback. No content is stored: the repo's
-- docs/releases/<slug>.html is the source of truth, the row records where it
-- was found and how the exchange ended. `completed` means delivered (the
-- callback was accepted), so completed_at and callback_sent_at are set together.
-- Mirrors reva/db/models.py::ReleaseNote.
CREATE TABLE IF NOT EXISTS release_notes (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT,
    odoo_instance_id BIGINT NOT NULL REFERENCES odoo_instances(id),
    release_id BIGINT NOT NULL,
    release_name TEXT NOT NULL,
    slug TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    source_repo_id BIGINT REFERENCES repositories(id),
    source_path TEXT,
    url TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    callback_sent_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_release_notes_created
    ON release_notes (created_at DESC);

-- "What did we answer for this release?" (TUI drill-down, support questions).
CREATE INDEX IF NOT EXISTS idx_release_notes_instance_release
    ON release_notes (odoo_instance_id, release_id);

-- One in-flight lookup per release: a re-submit while a job is running echoes
-- the pending note_id instead of racing it (Odoo overwrites reva_note_id on
-- every request, which would 409 the first job's delivery).
CREATE UNIQUE INDEX IF NOT EXISTS idx_release_notes_pending
    ON release_notes (odoo_instance_id, release_id)
    WHERE status = 'pending';
