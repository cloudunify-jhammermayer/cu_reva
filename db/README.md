# db/ — production schema

This directory holds the production database schema as plain-SQL migrations.

- **[`migrations/`](migrations)** — numbered `NNN_*.sql` files applied at
  worker/api/scheduler startup, tracked in `schema_migrations`. See its README
  for the runner's ordering, idempotency, and concurrency guarantees.

The matching SQLAlchemy models (and the runner itself) live in
[`../reva/db`](../reva/db). The SQL files are authoritative for the **live
Postgres** schema; the models are authoritative for the **SQLite test** schema.
They must stay in lockstep — when you add a migration, mirror it in `models.py`
(and vice versa).
