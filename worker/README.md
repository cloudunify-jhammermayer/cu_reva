# worker/ — REVA review worker

RQ-backed worker that consumes review jobs and produces Check Runs +
PR Reviews on GitHub plus persisted `review_runs` / `review_findings` rows
in Postgres.

After the shared-library extraction (slice 9a), this directory only
contains worker-specific orchestration glue. The reusable building blocks
— types, errors, clients, formatters, DB layer — live in [`../shared/reva/`](../shared/README.md).

## Modules in this package

| Module | Role |
|---|---|
| `worker/reviewer.py` | **Pure** orchestration. Defines the `GitHubReader` + `RepoLookup` Protocols. Reads diffs, calls Claude under the `submit_review` tool contract, validates findings, caps to 15 by severity × confidence, recomputes risk_level. Returns a `ReviewResult` — no side effects. |
| `worker/runner.py` | End-to-end side-effectful orchestration: `WorkerContext`, `build_worker_context(settings)`, `run_review(job_params)`. Idempotent on RQ retry. Posts Check Run + PR Review (or issue comment, for declines). |
| `worker/tasks.py` | Stable RQ enqueue path — re-exports `run_review` so `worker.tasks.run_review` stays valid even if internal layout changes. |
| `worker/settings.py` | Frozen `Settings` dataclass loaded from environment in `main.py`. |
| `worker/main.py` | Process entry point: load Settings → build context (runs DB migrations) → start RQ Worker on the `reviews` queue. |

## What you import from where

- `from reva.X import ...` — shared library: types, errors, claude_client, github_client, db.\*, review_formatter, prompt_builder, diff_utils, cost, review_tool.
- `from worker.X import ...` — worker-internal: reviewer, runner, tasks, settings.

The two are intentionally distinct: anything that another process (api, scheduler) might need lives in `shared/`. Anything specific to the RQ-worker lifecycle stays here.

## Running locally

```bash
brew install python@3.14
cd worker
/opt/homebrew/bin/python3.14 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt    # installs shared/ as editable + pytest
.venv/bin/python -m pytest tests/                  # 116 passing
```

Running the worker itself against real services requires the environment variables listed in `worker/settings.py` (`Settings.from_env`), plus a Redis and a Postgres reachable at the URLs you pass in. There is currently no Compose file at the repo root that wires this up — that's a future slice.

## Tests

See `tests/README.md`. All tests use fakes / `httpx.MockTransport` /
SQLite in-memory; no network or Docker required.

## Design constraints

- **`Reviewer` is pure.** Tests stay fast and Reviewer can be reused outside the worker context.
- **All writes are idempotent on natural keys.** RQ retries and webhook redeliveries can't duplicate.
- **`tasks.run_review` is the stable enqueue path.** Implementation can be reorganized without breaking in-flight jobs.
- **Retries belong to RQ, not the clients.** Clients raise `TransientError` / `PermanentError`; RQ decides what to retry.
