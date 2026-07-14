-- Repo-docs retrieval index (spec 2026-07-14): section-level FTS index of each
-- customer repo's custom-addon markdown docs, lazily synced from the repo's
-- DEFAULT branch at ticket-analysis time. The core-query planner's terms
-- retrieve relevant sections into an "Existing Customizations" analysis block.
-- FTS is Postgres-only by design (expression GIN index below); SQLite tests use
-- the ilike fallback in reva/repo_docs.py::search_repo_docs. Mirrors
-- reva/db/models.py (RepoDocSection, RepoDocsSync) + ticket_analyses column.

CREATE TABLE IF NOT EXISTS repo_doc_sections (
    id BIGSERIAL PRIMARY KEY,
    repo_full_name TEXT NOT NULL,          -- lowercased "owner/repo"
    path TEXT NOT NULL,
    anchor TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_repo_doc_sections_repo ON repo_doc_sections (repo_full_name);
CREATE INDEX IF NOT EXISTS idx_repo_doc_sections_fts ON repo_doc_sections
    USING GIN (to_tsvector('english', title || ' ' || body));

CREATE TABLE IF NOT EXISTS repo_docs_sync (
    repo_full_name TEXT PRIMARY KEY,
    tree_sha TEXT NOT NULL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    files INTEGER NOT NULL DEFAULT 0,
    sections INTEGER NOT NULL DEFAULT 0,
    truncated BOOLEAN NOT NULL DEFAULT FALSE
);

ALTER TABLE ticket_analyses ADD COLUMN IF NOT EXISTS repo_docs_sections_used INTEGER;
