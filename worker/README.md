# worker/ — REVA job worker

RQ-backed worker that consumes jobs from Redis and produces the side effects:
GitHub Check Runs + PR Reviews, Postgres `review_runs` / `review_findings`
rows, Odoo ticket/timesheet callbacks, and Google Chat alerts.

The reusable building blocks — types, errors, the two Claude clients, the
GitHub client, formatters, and the DB layer — live in the installable
[`reva/`](../reva) package (imported as `from reva.X import ...`). This
directory holds only worker-specific orchestration glue.

## Modules

| Module | Role |
|---|---|
| `worker/reviewer.py` | **Pure** PR-review orchestration. Defines the `GitHubReader` + `RepoLookup` Protocols. Fetches the diff — for a delta review, the GitHub *compare* diff when the new head descends from the last reviewed SHA, or a **local two-tree `git diff <prior> <new>`** (via `ClaudeCodeRunner.two_tree_diff`, lock-free, degrade-in-place) when it *diverged* (force-push/amend) and the merge-base with the target is unchanged. On a PR's first review, may **carry a prior identical review forward** (see the root README's "Incremental & carried-forward reviews"), returning a completed `ReviewResult` with no Claude run. Otherwise applies size/skip guards, clones via `ClaudeCodeRunner`, runs the headless CLI under a per-repo lock, validates findings, caps to 15 by severity × confidence, recomputes `risk_level`. Returns a `ReviewResult` — no DB writes, no GitHub posts. |
| `worker/auditor.py` | **Pure** full-repo audit orchestration. Clones the default branch and runs the `reva-repo-audit` skill, always on the deep model (Opus 4.8), with CodeGraph when enabled. Returns an `AuditResult`. |
| `worker/runner.py` | All the **side effects**: `WorkerContext`, `build_worker_context(settings)`, and the RQ entry points `run_review`, `run_comment_reply`. Idempotent on retry: persists each GitHub ID immediately after posting, and if a prior attempt crashed *between* the GitHub create and that DB write, recovers the existing PR review (by `Run #<id>` marker) / Check Run (by name on the head SHA) from GitHub instead of duplicating; skips a job whose Check Run already posted (excluding `failed` runs). Enforces the rolling daily spend cap (serialized via a Postgres advisory lock). Also runs the delta-review finding-resolution pass. |
| `worker/ticket_runner.py` | `run_ticket_analysis` — Odoo ticket analysis via the Messages API (`TicketAnalyzer`), grounded in retrieved Odoo core knowledge + the customer repo's own docs (`reva/ticket_knowledge.py`), then write-back to Odoo. |
| `worker/timesheet_runner.py` | `run_timesheet_review` — Odoo timesheet wording review via the Messages API (`TimesheetAnalyzer`), persisted in chunks and callbacked to `/hr/timesheet-results`. |
| `worker/audit_tasks.py` | `run_audit` — persists audit lifecycle rows and invokes `Auditor`. Persists **every** finding to `audit_findings`, and opens a GitHub issue for each **MAJOR/CRITICAL** finding (title `[REVA audit] <title>`, label `reva-audit` auto-created per repo, deduped across re-runs via a hidden marker — skipped if a matching open issue exists). Lower-severity findings are stored but not issued. Issue creation is best-effort (logs `audit_issue_created` / `audit_issue_failed`, never fails the audit); requires GitHub App `Issues: Read & write`. |
| `worker/tasks.py`, `worker/ticket_tasks.py`, `worker/timesheet_tasks.py` | **Stable enqueue paths** — thin re-exports so `worker.tasks.run_review` / `worker.ticket_tasks.run_ticket_analysis` / `worker.timesheet_tasks.run_timesheet_review` stay valid even if internal layout changes. |
| `worker/settings.py` | Frozen `Settings` dataclass; `Settings.from_env()`. |
| `worker/main.py` | Process entry: load settings → `build_worker_context` (runs DB migrations + prunes stale repo clones) → start the RQ `Worker` on the configured queue. |

## Job types (all on one queue)

| Enqueue path | Enqueued by | Client used |
|---|---|---|
| `worker.tasks.run_review` | scheduler poller (after debounce) | headless Claude Code CLI |
| `worker.runner.run_comment_reply` | api `pull_request_review_comment` webhook | Messages API |
| `worker.ticket_tasks.run_ticket_analysis` | Odoo / ticket trigger | Messages API |
| `worker.timesheet_tasks.run_timesheet_review` | Odoo `/api/v1/timesheet-review` | Messages API |
| `worker.audit_tasks.run_audit` | api `POST /repos/{id}/audit`, TUI | headless Claude Code CLI |
| `worker.board_status_tasks.run_board_status_update` | api `pull_request` webhook (PR activity), `worker/runner.py` (completed review) | no Claude — GitHub Projects card moves for linked REVA issues, fail-soft |

## Models

Model names come from a single source, [`reva/config.py`](../reva/config.py),
and are env-overridable:

| Env var | Default | Used by |
|---|---|---|
| `REVA_DEFAULT_MODEL` | `claude-sonnet-4-6` | standard reviews |
| `REVA_DEEP_MODEL` | `claude-opus-4-8` | `/deep-review` and all repo audits |

## CodeGraph (optional, off by default)

When enabled, the worker indexes the clone with the `codegraph` binary and
exposes a read-only MCP server (`mcp__codegraph__*`) to repo-aware skills only
(full/deep reviews + audits). Fail-silent: on success logs `codegraph_index_ready`
(mode `init|sync`); on failure logs `codegraph_index_skipped` / `codegraph_index_failed`.

| Env var | Default |
|---|---|
| `REVA_CODEGRAPH_ENABLED` | `false` |
| `REVA_CODEGRAPH_INDEX_TIMEOUT` | `180` |

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
