-- Per-instance quotas: NULL = unlimited, so existing instances behave exactly
-- as before until an operator sets a cap. daily_budget_usd gates rolling-24h
-- spend summed over the instance-scoped run tables; rate_limit_per_minute caps
-- create-route requests. Mirrors reva/db/models.py::OdooInstance.
ALTER TABLE odoo_instances ADD COLUMN IF NOT EXISTS daily_budget_usd NUMERIC(12, 2);
ALTER TABLE odoo_instances ADD COLUMN IF NOT EXISTS rate_limit_per_minute INTEGER;
