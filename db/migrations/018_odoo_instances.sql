-- Each Odoo instance that talks to REVA: its REVA-minted inbound key (stored
-- hashed) and its own outbound callback target (URL + Fernet-encrypted key).
CREATE TABLE IF NOT EXISTS odoo_instances (
    id                    BIGSERIAL PRIMARY KEY,
    name                  TEXT NOT NULL UNIQUE,
    key_hash              TEXT NOT NULL UNIQUE,
    key_prefix            TEXT NOT NULL,
    callback_url          TEXT NOT NULL DEFAULT '',
    callback_api_key_enc  TEXT NOT NULL DEFAULT '',
    active                BOOLEAN NOT NULL DEFAULT true,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Source-instance scoping on the two ticket tables. App-required (every new row
-- is stamped at create time); nullable at the DB level only because Postgres
-- can't add a NOT NULL column without a default to a possibly-non-empty table.
ALTER TABLE ticket_analyses   ADD COLUMN IF NOT EXISTS odoo_instance_id BIGINT REFERENCES odoo_instances(id);
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS odoo_instance_id BIGINT REFERENCES odoo_instances(id);

-- One in-flight create-issues run PER INSTANCE per (ticket_id, model_name).
-- Replaces the single-Odoo index so two instances may each have a pending run
-- for the same ticket_id.
DROP INDEX IF EXISTS idx_ticket_issue_runs_pending;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ticket_issue_runs_pending
    ON ticket_issue_runs (odoo_instance_id, ticket_id, model_name)
    WHERE status = 'pending';
