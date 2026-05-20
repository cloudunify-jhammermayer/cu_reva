# worker/tests/ — Test suite

Fast, deterministic, no network. **116 tests** as of HANDOFF slice 9a (post `shared/` extraction).

## Suites

| File | Cases | What it covers |
|---|---|---|
| `test_claude_client.py` | 12 | Anthropic Messages API client: tool_use parsing, cache-token accounting, status mapping. Uses `httpx.MockTransport`. |
| `test_github_client.py` | 25 | GitHub App API client: JWT signing (against a real RSA key), installation-token cache, paginated reads, write methods, error mapping. |
| `test_reviewer.py` | 20 | Pure Reviewer orchestration with in-memory fakes for GitHub / RepoLookup / Claude / PromptBuilder. |
| `test_runner.py` | 10 | End-to-end orchestration (`run_review`) against a real SQLite DB + fake clients. Covers all four review statuses, idempotent retry, error paths. |
| `test_db.py` | 17 | DB writers + upserts + migration runner on `sqlite:///:memory:`. |
| `test_review_formatter.py` | 17 | Pure formatter functions: conclusion matrix, body templates, severity emoji, decline message. |
| `test_diff_utils.py` | 7 | Unified-diff line counting + hunk parsing + line-in-hunk lookup. |
| `test_prompt_files.py` | 8 | Real `prompts/` directory loads correctly through `PromptBuilder`. |

## Running

```bash
cd worker
.venv/bin/python -m pytest tests/
```

Add `-v` for per-test status, `-k <name>` to run a subset, `--lf` to re-run
only the failures from the last run.

## Conventions

- **No live API.** Network access is mocked via `httpx.MockTransport`. Tests must not depend on the internet.
- **No Docker.** SQLite in-memory replaces Postgres for tests. Trade-off documented in HANDOFF.md.
- **One concern per test.** Most tests are < 20 lines.
- **Fakes are dataclasses with counters.** Easier to assert on than `unittest.mock` and reads like a small simulation.

## Adding a new test suite

If you add a new module:

- Worker-only orchestration (touches RQ, Reviewer, Settings) → `worker/worker/`, test here.
- Reusable building block (could be imported by api/scheduler) → `shared/reva/`, test in `shared/` (no test dir there yet — currently all `reva.*` tests live in `worker/tests/` for ergonomics, which is fine until the api/scheduler grow their own test suites).

`conftest.py` adds both `worker/` and `shared/` to `sys.path` so
`from worker...` and `from reva...` both resolve in tests without
needing the editable pip install.
