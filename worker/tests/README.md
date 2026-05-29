# worker/tests/ — test suite

Fast, deterministic, no network or Docker. **197 tests.** Covers both the
worker orchestration and the shared `reva.*` library (the api and scheduler
have their own suites).

## Suites

| File | Cases | What it covers |
|---|---|---|
| `test_reviewer.py` | 23 | Pure PR-review orchestration with fakes for GitHub / RepoLookup / runner / prompts. Diff + delta paths, size guards, capping, per-repo lock. |
| `test_runner.py` | 17 | End-to-end `run_review` against SQLite + fake clients: all statuses, idempotent retry, no-duplicate-PR-review on partial-post retry, failed-run requeue, error paths. |
| `test_auditor.py` | 4 | Pure repo-audit orchestration. |
| `test_claude_code_runner.py` | 21 | Headless CLI runner: clone/fetch with token-less remote, env minimization, per-repo lock, output-file parsing, exit-code → error mapping, stale-repo eviction. Subprocess mocked. |
| `test_claude_client.py` | 12 | Messages API client: tool_use parsing, cache-token accounting, status mapping. `httpx.MockTransport`. |
| `test_github_client.py` | 28 | GitHub App client: JWT signing (real RSA key), token cache, paginated reads, writes, error mapping. |
| `test_odoo_client.py` | 7 | Odoo JSON-RPC client. |
| `test_ticket_analyzer.py` / `test_ticket_runner.py` | 8 / 7 | Ticket analysis + its RQ runner / write-back. |
| `test_finding_verifier.py` | 3 | "Is this prior finding resolved?" verifier. |
| `test_db.py` | 29 | DB writers, upserts, conflict-retry decorator, migration runner. `sqlite:///:memory:`. |
| `test_review_formatter.py` | 17 | Pure formatters: conclusion matrix, body templates, decline message. |
| `test_diff_utils.py` | 13 | Diff filtering, line counting, hunk parsing. |
| `test_prompt_files.py` | 8 | Real `prompts/` loads through `PromptBuilder`. |

## Running

```bash
cd worker && .venv/bin/python -m pytest tests/
```

`-v` for per-test status, `-k <name>` to filter, `--lf` to re-run last failures.

## Conventions & why

- **No live API / no Docker.** Network is mocked (`httpx.MockTransport`), the
  Claude CLI is mocked at `subprocess.run`, and SQLite in-memory replaces
  Postgres — so the suite runs in ~1 s and needs no services. Trade-off:
  Postgres-only behaviour (advisory lock, partial-index `WHERE`) isn't exercised
  here.
- **Fakes are dataclasses with counters** — reads like a small simulation and is
  easy to assert on.
- `conftest.py` puts the project root on `sys.path` so `from reva.X import ...`
  and `from worker.X import ...` both resolve without the editable install.

`reva.*` building blocks are tested here for ergonomics; if the api/scheduler
grow heavier logic they get their own suites (they already have basic ones).
