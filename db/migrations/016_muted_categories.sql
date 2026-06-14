-- Per-repo muted finding categories (Tier 3 / feature A). A trusted user mutes a
-- category via `/mute <category>` on an inline comment; the reviewer then drops
-- findings in that category before posting. `/unmute` sets active = false. The
-- unique constraint makes mute/unmute an idempotent upsert per (repo, category).
CREATE TABLE IF NOT EXISTS muted_categories (
    id            BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    category      TEXT   NOT NULL,
    muted_by      TEXT   NOT NULL,
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repository_id, category)
);
CREATE INDEX IF NOT EXISTS idx_muted_categories_repo
    ON muted_categories(repository_id) WHERE active;
