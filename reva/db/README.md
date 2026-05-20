# shared/reva/db/ — Database layer

SQLAlchemy 2.0 models, session management, plain-SQL migration runner, and
the writer helpers that `worker.runner.run_review` and the api/scheduler call.

Lives in `shared/` so any process can talk to Postgres without depending on
the worker package.

## Modules

| File | Role |
|---|---|
| `__init__.py` | Public API: `Database`, `DatabaseRepoLookup`, `writers`, models, `migrate`, `create_engine_from_url` |
| `engine.py` | `create_engine_from_url`, `Database` facade with context-managed sessions, `migrate(engine, dir)` runner |
| `models.py` | 9 typed declarative models for all REVA tables; PK uses `BigInteger().with_variant(Integer, "sqlite")` so SQLite tests autoincrement correctly |
| `repo_lookup.py` | `DatabaseRepoLookup` adapter — implements the `worker.reviewer.RepoLookup` Protocol |
| `writers.py` | Idempotent writers: `record_review_started/completed/declined/stale/failed`, `attach_github_ids`, `is_already_posted`, `upsert_repository`, `upsert_pull_request`, `upsert_pending_review`, `record_github_event` |

## Idempotency

Every writer is idempotent on a natural key so RQ retries and webhook
redeliveries don't duplicate state:

| Table | Natural key |
|---|---|
| `repositories` | `github_repository_id` |
| `pull_requests` | `(repository_id, pr_number)` |
| `pending_reviews` | `(repository_id, pr_number)` — also the debounce mechanism |
| `review_runs` | `(repository_id, pull_request_id, head_sha, review_mode)` |
| `github_events` | `delivery_id` |

`is_already_posted(db, params)` returns True iff a `review_runs` row exists
with `check_run_id` set — used by `worker.runner.run_review` to skip the
post step on RQ retry.

## Session lifecycle

```python
with db.session() as s:
    s.execute(...)
    # commit happens on clean exit; rollback on exception
```

Sessions are short-lived (one per write helper call). Callers that need a
longer transaction can open `db.session()` directly.

## Migrations

The SQL files in `/db/migrations/` (at the repo root) are the production
deploy path. They run once at worker / api / scheduler process startup,
tracked in `schema_migrations`. The migration runner skips files whose
version is already applied; a second `migrate()` call is a no-op.

SQLite tests **do not** run the SQL migrations (they use Postgres-only
`BIGSERIAL`); they call `Base.metadata.create_all(engine)` instead.
The models are the source of truth for the live schema; the SQL files
must stay in lockstep.

## Why SQLAlchemy

Chosen for familiarity and ergonomic session management. The flat 9-table
schema didn't strictly need an ORM — `psycopg` + raw SQL would have worked
too. See HANDOFF.md → "ORM" decision row for the trade-off.
