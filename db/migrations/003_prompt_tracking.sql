-- Prompt version tracking
CREATE TABLE prompt_versions (
    id BIGSERIAL PRIMARY KEY,
    version TEXT UNIQUE NOT NULL,
    system_prompt_hash TEXT NOT NULL,
    review_prompt_hash TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
