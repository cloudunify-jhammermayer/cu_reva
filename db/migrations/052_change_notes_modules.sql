-- The Odoo modules a merged PR touched and the git submodules it moved (spec
-- 2026-09-29-change-summary-modules). modules: JSON list of technical names,
-- the directories directly under custom_addons/. submodules: JSON list of
-- submodule paths. Both are written together.
-- NULL = never looked up (rows from before this migration, or a failed lookup);
-- [] = looked up, nothing found.
-- Mirrors reva/db/models.py::ChangeNote.modules / .submodules.
ALTER TABLE change_notes ADD COLUMN IF NOT EXISTS modules JSONB;
ALTER TABLE change_notes ADD COLUMN IF NOT EXISTS submodules JSONB;
