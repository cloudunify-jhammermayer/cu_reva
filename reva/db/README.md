# reva/db/ — database layer

SQLAlchemy 2.0 models, session management, the plain-SQL migration runner, and
the idempotent writer/reader helpers used by the worker, api, and scheduler.

Lives in the shared [`reva/`](..) package so any process can talk to Postgres
without depending on the worker.

## Modules

| File | Role |
|---|---|
| `__init__.py` | Public API: `Database`, `DatabaseRepoLookup`, `writers`, models, `migrate`, `create_engine_from_url` |
| `engine.py` | `create_engine_from_url`, the `Database` facade (context-managed sessions, commit-on-exit / rollback-on-exception), and `migrate(engine, dir)`. |
| `models.py` | Typed declarative models for every table (repositories, pull_requests, pending_reviews, review_runs, review_findings, github_events, review_jobs, review_feedback, ticket_analyses, audit_runs, prompt_versions, weekly_reports). PK uses `BigInteger().with_variant(Integer, "sqlite")` so SQLite tests autoincrement. Partial / `DESC` indexes carry `postgresql_where` / explicit ordering to mirror the migrations. |
| `repo_lookup.py` | `DatabaseRepoLookup` — implements the `reviewer.RepoLookup` Protocol (owner/name, PR basics, last completed review). |
| `writers.py` | Idempotent writers + reads: `record_review_started/completed/declined/stale/failed`, `attach_github_ids`, `is_already_posted`, `get_posted_github_ids`, the `upsert_*` webhook entries, `record_github_event`, finding-comment lookups, and ticket/audit writers. |

## Idempotency — and why it matters

RQ retries and GitHub webhook redeliveries can fire the same write more than
once. Every writer is idempotent on a natural key:

| Table | Natural key |
|---|---|
| `repositories` | `github_repository_id` |
| `pull_requests` | `(repository_id, pr_number)` |
| `pending_reviews` | `(repository_id, pr_number)` — also the debounce mechanism |
| `review_runs` | `(repository_id, pull_request_id, head_sha, review_mode)` |
| `github_events` | `delivery_id` |

`is_already_posted(db, params)` returns True only when a **successfully posted**
run exists (`check_run_id` set and status ≠ `failed`) — so a requeue of a
previously-failed review still runs. `get_posted_github_ids` lets the post path
reuse an already-created PR review on retry instead of duplicating it.

The SELECT-then-INSERT upserts have a TOCTOU window under concurrent webhook
deliveries; the `_retry_on_conflict` decorator retries once on the unique-key
`IntegrityError`, where the retry takes the UPDATE branch.

## Migrations

The SQL files in [`/db/migrations/`](../../db/migrations) are the production
schema. `migrate()` runs at worker/api/scheduler startup, tracks applied
versions in `schema_migrations`, and is a no-op once current. On Postgres it
holds an advisory lock so two processes starting at once can't race the same
DDL; migration bodies run via `exec_driver_sql` (multi-statement DDL, no `:`
bind-param misparse).

SQLite tests **do not** run the SQL migrations — they call
`Base.metadata.create_all(engine)`. The models are therefore the source of
truth for the schema and **must stay in lockstep with the SQL files**.

## Why SQLAlchemy

Chosen for ergonomic, context-managed sessions and a single typed schema
definition. The flat schema didn't strictly need an ORM (`psycopg` + raw SQL
would also work); see `HANDOFF.md` → "ORM" decision row for the trade-off.
