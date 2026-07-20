-- Default Odoo instance for the no-linked-issue PR fallback (spec 2026-07-20):
-- extracted ticket ids REVA has never seen resolve to this instance. At most
-- one default — enforced by the partial unique index. Setting the flag is a
-- manual deploy step (the migration cannot know which row):
--   UPDATE odoo_instances SET is_default = TRUE WHERE name = '<instance-name>';
-- Mirrors reva/db/models.py::OdooInstance.is_default.
ALTER TABLE odoo_instances ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE;

CREATE UNIQUE INDEX IF NOT EXISTS uq_odoo_instances_default
    ON odoo_instances (is_default) WHERE is_default;
