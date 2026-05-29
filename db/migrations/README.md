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
| 001 | `001_initial.sql` | Core tables: `repositories`, `pull_requests`, `pending_reviews`, `review_runs`, `review_findings`, `github_events`, `review_jobs`, plus indexes |
| 002 | `002_feedback.sql` | `review_feedback` table for 👍/👎 reactions on REVA's comments |
| 003 | `003_prompt_tracking.sql` | `prompt_versions` table for A/B comparison of prompt revisions |
| 004 | `004_comment_id_index.sql` | Partial index on `review_findings(github_comment_id)` to match reply webhooks to findings |
| 005 | `005_weekly_reports.sql` | `weekly_reports` table — dedups weekly-report sends across scheduler restarts |
| 006 | `006_ticket_analyses.sql` | `ticket_analyses` table for Odoo ticket analysis (partial unique index on `job_id`) |
| 007 | `007_audit_runs.sql` | `audit_runs` table for on-demand full-repo audits |

## Adding a migration

1. Create `db/migrations/00N_short_name.sql` where N is `max(existing) + 1`.
2. Write **forward-only** DDL. No rollback support; design with safe defaults so partial failure is recoverable.
3. Update `reva/db/models.py` in the same change so the SQLAlchemy models match (including index ordering / partial `WHERE`, so the SQLite test schema mirrors prod).
4. Run against a real Postgres before merging — the SQLite test suite won't catch Postgres-specific syntax or multi-statement files.

## Runner

Implemented in [`reva/db/engine.py`](../../reva/db/engine.py) `migrate()`, called
at worker, api, **and** scheduler startup. Each file runs in its own
transaction via `exec_driver_sql` (handles multi-statement DDL; no `:`
bind-param misparse). On Postgres the whole run is serialized by an advisory
lock so concurrent startups can't race the same DDL; an already-applied
migration from a peer is detected and skipped.

## Schema authority

The `.sql` files are authoritative for the **live Postgres** schema; the
`reva/db/models.py` models are authoritative for the **SQLite test** schema
(tests use `create_all`, not these files). The two must stay in sync.
