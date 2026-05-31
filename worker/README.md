# worker/ — REVA job worker

RQ-backed worker that consumes jobs from Redis and produces the side effects:
GitHub Check Runs + PR Reviews, Postgres `review_runs` / `review_findings`
rows, Odoo ticket write-backs, and Google Chat alerts.

The reusable building blocks — types, errors, the two Claude clients, the
GitHub client, formatters, and the DB layer — live in the installable
[`reva/`](../reva) package (imported as `from reva.X import ...`). This
directory holds only worker-specific orchestration glue.

## Modules

| Module | Role |
|---|---|
| `worker/reviewer.py` | **Pure** PR-review orchestration. Defines the `GitHubReader` + `RepoLookup` Protocols. Fetches the diff (or the *compare* diff vs the last completed review, for delta reviews), applies size/skip guards, clones the repo via `ClaudeCodeRunner`, runs the headless CLI under a per-repo lock, validates findings, caps to 15 by severity × confidence, recomputes `risk_level`. Returns a `ReviewResult` — no DB writes, no GitHub posts. |
| `worker/auditor.py` | **Pure** full-repo audit orchestration. Clones the default branch and runs the `reva-repo-audit` skill. Returns an `AuditResult`. |
| `worker/runner.py` | All the **side effects**: `WorkerContext`, `build_worker_context(settings)`, and the RQ entry points `run_review`, `run_comment_reply`. Idempotent on retry: persists each GitHub ID immediately after posting, and if a prior attempt crashed *between* the GitHub create and that DB write, recovers the existing PR review (by `Run #<id>` marker) / Check Run (by name on the head SHA) from GitHub instead of duplicating; skips a job whose Check Run already posted (excluding `failed` runs). Enforces the rolling daily spend cap (serialized via a Postgres advisory lock). Also runs the delta-review finding-resolution pass. |
| `worker/ticket_runner.py` | `run_ticket_analysis` — Odoo ticket analysis via the Messages API (`TicketAnalyzer`), then write-back to Odoo. |
| `worker/audit_tasks.py` | `run_audit` — persists audit lifecycle rows and invokes `Auditor`. |
| `worker/tasks.py`, `worker/ticket_tasks.py` | **Stable enqueue paths** — thin re-exports so `worker.tasks.run_review` / `worker.ticket_tasks.run_ticket_analysis` stay valid even if internal layout changes. |
| `worker/settings.py` | Frozen `Settings` dataclass; `Settings.from_env()`. |
| `worker/main.py` | Process entry: load settings → `build_worker_context` (runs DB migrations + prunes stale repo clones) → start the RQ `Worker` on the configured queue. |

## Job types (all on one queue)

| Enqueue path | Enqueued by | Client used |
|---|---|---|
| `worker.tasks.run_review` | scheduler poller (after debounce) | headless Claude Code CLI |
| `worker.runner.run_comment_reply` | api `pull_request_review_comment` webhook | Messages API |
| `worker.ticket_tasks.run_ticket_analysis` | Odoo / ticket trigger | Messages API |
| `worker.audit_tasks.run_audit` | api `POST /repos/{id}/audit`, TUI | headless Claude Code CLI |

## Why it's built this way

- **Pure `Reviewer` / `Auditor`.** Keeping the LLM orchestration free of DB and
  GitHub side effects makes it fast to unit-test with fakes and lets the same
  code run outside the worker. Side effects are concentrated in `runner.py`.
- **Idempotent on natural keys.** RQ retries and webhook redeliveries must not
  duplicate Check Runs, PR reviews, or rows. The create→persist crash window is
  closed by recovering the existing object from GitHub on retry. See
  [`../reva/db/README.md`](../reva/db/README.md).
- **Bounded work.** The headless CLI subprocess and every `git` clone/fetch run
  under timeouts, and the RQ job timeout is derived from the subprocess timeout
  (always larger), so a long review is never SIGKILLed mid-run. A worker killed
  anyway leaves a `running` row that the scheduler's reaper later fails.
- **Retries belong to RQ, not the clients.** Clients raise `TransientError` /
  `PermanentError`; RQ decides what to retry (`Retry(max=3, interval=[30,120,300])`).
- **Stable enqueue paths.** Producers (api, scheduler) reference fixed import
  strings; internal modules can be reorganized without breaking in-flight jobs.

## Running locally

```bash
cd worker
python3 -m venv .venv          # Python 3.14
.venv/bin/pip install -r requirements-dev.txt   # installs ../reva editable + pytest
.venv/bin/python -m pytest tests/               # 241 passing
```

Running the worker against real services needs the env vars in
`worker/settings.py` plus a reachable Redis + Postgres, the `claude` CLI on
`PATH`, and `git`. Use the repo-root `docker-compose.yml`, which wires all of
that up. Tests need none of it — see `tests/README.md`.
