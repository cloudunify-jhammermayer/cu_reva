-- Support-answer personas (spec 2026-07-25): the tone REVA writes a customer-
-- facing draft in, resolved per field as default < repo < the additive
-- persona_context Odoo sends per request.
--
-- Keyed on repo_full_name TEXT, deliberately NOT an FK to repositories: a
-- support request can name a repo REVA has never received a webhook for, so an
-- FK would reject a legitimate persona. repo_doc_sections keys the same way.
--
-- Two partial unique indexes: at most one 'default' row (the fallback used by
-- project-less requests) and at most one row per repo. Mirrors
-- reva/db/models.py::Persona.

CREATE TABLE IF NOT EXISTS personas (
    id BIGSERIAL PRIMARY KEY,
    scope TEXT NOT NULL,                   -- 'default' | 'repo'
    repo_full_name TEXT,                   -- lowercased "owner/repo"; NULL when scope='default'
    language TEXT,                         -- 'auto' | 'de' | 'en'
    formality TEXT,                        -- 'formal' | 'informal'  (the Sie/du axis)
    technical_depth TEXT,                  -- 'low' | 'medium' | 'high'
    length TEXT,                           -- 'brief' | 'standard' | 'detailed'
    salutation TEXT,
    sign_off TEXT,
    style_notes TEXT,
    -- Kept separate from style_notes so it renders as a hard constraint in the
    -- prompt ("never quote prices", "never commit to a delivery date") instead
    -- of being buried in prose the model may treat as tone guidance.
    content_policy TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_personas_repo
    ON personas (repo_full_name) WHERE scope = 'repo';

CREATE UNIQUE INDEX IF NOT EXISTS uq_personas_default
    ON personas (scope) WHERE scope = 'default';
