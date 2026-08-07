-- Scope-version stamp for the repo-docs index (spec 2026-08-07). The doc scope
-- widened to include each repo's root docs/ folder, which does not move the
-- GitHub tree SHA that sync staleness keys on — so an already-indexed repo
-- would never pick the new files up. reva/repo_docs.py::_SCOPE_VERSION is
-- compared against this column, making every repo stale exactly once per scope
-- change. Default 0 = indexed before the stamp existed.
-- Mirrors reva/db/models.py (RepoDocsSync).
ALTER TABLE repo_docs_sync ADD COLUMN IF NOT EXISTS scope_version INTEGER NOT NULL DEFAULT 0;
