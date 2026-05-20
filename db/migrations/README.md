# db/migrations/ — Postgres migrations

Plain SQL migrations applied at worker / api startup. No Alembic — `doc/03`
chose explicit SQL for readability and full Postgres control.

## Naming convention

`<NNN>_<short_name>.sql`

The leading integer is the **version**. The migration runner sorts files by
that integer, applies any not yet recorded in `schema_migrations(version)`,
and skips the rest.

## Current migrations

| Version | File | What it does |
|---|---|---|
| 001 | `001_initial.sql` | All core tables: `repositories`, `pull_requests`, `pending_reviews`, `review_runs`, `review_findings`, `github_events`, `review_jobs`, plus indexes |
| 002 | `002_feedback.sql` | `review_feedback` table for tracking 👍/👎 reactions on REVA's comments |
| 003 | `003_prompt_tracking.sql` | `prompt_versions` table for A/B comparison of prompt revisions (no writer code yet) |

## Adding a migration

1. Create `db/migrations/00N_short_name.sql` where N is `max(existing) + 1`.
2. Write **forward-only** DDL. We don't support rollback in MVP; design with safe defaults so partial failure is recoverable.
3. Update `worker/worker/db/models.py` in the same change so the SQLAlchemy models match.
4. Run the migration locally against a real Postgres before merging (the SQLite test suite won't catch Postgres-specific syntax).

## Runner

Implemented in `worker/worker/db/engine.py::migrate()`. Called once at worker
process startup from `worker/worker/main.py`; future api startup will do the
same. Each file is applied inside its own transaction.

## Schema authority

For day-to-day reasoning about the live schema, look at
`worker/worker/db/models.py`. For deployment, the `.sql` files are
authoritative — they're what touches Postgres. The two must stay in sync;
diverging is a bug.
