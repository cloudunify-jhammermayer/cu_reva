-- Odoo core-knowledge registry (spec §2): deterministic extract of the
-- operator-provided core/enterprise/docs worktrees, loaded per version by
-- `python -m reva.odoo_registry load`. Read by ticket retrieval and the
-- diff-path core_overlap hints. FTS is Postgres-only by design (expression
-- GIN indexes below); SQLite tests use the query helper's LIKE fallback.
-- Mirrors reva/db/models.py (OdooCoreModule/Model/Field, OdooDocsSection,
-- CoreKnowledgeVersion) + odoo_instances.odoo_version.

CREATE TABLE IF NOT EXISTS odoo_core_modules (
    id BIGSERIAL PRIMARY KEY,
    odoo_version TEXT NOT NULL,
    module TEXT NOT NULL,
    source TEXT NOT NULL,
    category TEXT,
    summary TEXT,
    depends JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_core_modules_version ON odoo_core_modules (odoo_version, module);

CREATE TABLE IF NOT EXISTS odoo_core_models (
    id BIGSERIAL PRIMARY KEY,
    odoo_version TEXT NOT NULL,
    model TEXT NOT NULL,
    module TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_path TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_core_models_version_model ON odoo_core_models (odoo_version, model);

CREATE TABLE IF NOT EXISTS odoo_core_fields (
    id BIGSERIAL PRIMARY KEY,
    odoo_version TEXT NOT NULL,
    model TEXT NOT NULL,
    field TEXT NOT NULL,
    ftype TEXT,
    module TEXT NOT NULL,
    string TEXT,
    compute TEXT,
    related TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_core_fields_version_model ON odoo_core_fields (odoo_version, model);

CREATE TABLE IF NOT EXISTS odoo_docs_sections (
    id BIGSERIAL PRIMARY KEY,
    odoo_version TEXT NOT NULL,
    path TEXT NOT NULL,
    anchor TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_docs_sections_version ON odoo_docs_sections (odoo_version);
CREATE INDEX IF NOT EXISTS idx_docs_sections_fts ON odoo_docs_sections
    USING GIN (to_tsvector('english', title || ' ' || body));

CREATE TABLE IF NOT EXISTS core_knowledge_versions (
    odoo_version TEXT PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    modules INTEGER NOT NULL DEFAULT 0,
    models INTEGER NOT NULL DEFAULT 0,
    fields INTEGER NOT NULL DEFAULT 0,
    sections INTEGER NOT NULL DEFAULT 0
);

ALTER TABLE odoo_instances ADD COLUMN IF NOT EXISTS odoo_version TEXT;
