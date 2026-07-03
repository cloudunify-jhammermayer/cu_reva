-- Per-repo learned review memory (Tier 3 / feature B). A scheduled distiller
-- turns a repo's dismissed-finding history into a short "what this team tends to
-- reject" guidance block, injected into review prompts as team_review_preferences.
-- Append-only versions: writing version N+1 deactivates N in the same transaction,
-- so exactly one row per repo is active. content "" means nothing to inject.
CREATE TABLE IF NOT EXISTS repo_review_memory (
    id                 BIGSERIAL PRIMARY KEY,
    repository_id      BIGINT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    version            INTEGER NOT NULL,
    content            TEXT NOT NULL,
    items              JSON,
    source_stats       JSON,
    model              TEXT,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    estimated_cost_usd NUMERIC(12,6),
    active             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repository_id, version)
);
CREATE INDEX IF NOT EXISTS idx_repo_review_memory_active
    ON repo_review_memory(repository_id) WHERE active;

-- Stamp which learned-memory version a review injected, so dismiss-rate trends
-- can later be attributed to memory versions.
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS learned_memory_version INTEGER;
