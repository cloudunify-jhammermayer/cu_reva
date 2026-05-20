# shared/ — `reva` library

Code shared between the worker, the api, and the scheduler. Anything that
isn't process-specific orchestration lives here.

Installable as a pip package (`reva-shared`) so each consumer's Dockerfile
can `pip install ./shared` and get a versioned dep with its own deps pinned.

## Layout

```
shared/
├── pyproject.toml          installable package metadata
├── README.md
└── reva/
    ├── __init__.py
    ├── types.py            Pydantic data contracts (Finding, ReviewResult, JobParams, ClaudeResponse, ContentBlock)
    ├── errors.py           TransientError, PermanentError, StaleHeadError, DeclinedError, WorkerError
    ├── review_tool.py      submit_review Claude tool schema, derived from ReviewResult
    ├── claude_client.py    Anthropic Messages API client (tool_use + cache-token accounting)
    ├── github_client.py    GitHub App API client — reads (PR/diff/files/contents) + writes (Check Run, PR Review, issue comment)
    ├── _github_http.py     shared GitHub error mapping (used by reader + writer paths)
    ├── prompt_builder.py   builds cache-tagged system blocks + user prompts from /app/prompts
    ├── review_formatter.py pure templates: conclusion matrix, Check Run output, PR review body, inline comments, decline body
    ├── diff_utils.py       count_diff_lines, estimate_diff_tokens, parse_diff_hunks, find_line_in_hunks
    ├── cost.py             placeholder pricing for Sonnet 4.6 / Opus 4.7 + estimate_cost()
    └── db/
        ├── __init__.py     public API: Database, DatabaseRepoLookup, writers, models, migrate
        ├── engine.py       create_engine_from_url + Database facade + migrate() runner
        ├── models.py       9 SQLAlchemy 2.0 typed declarative models
        ├── repo_lookup.py  DatabaseRepoLookup adapter (satisfies worker.reviewer.RepoLookup)
        └── writers.py      idempotent CRUD helpers — record_review_*, attach_github_ids, upsert_*, record_github_event
```

## Design boundary

This package is **pure** in the sense of "no queue side effects." It has:

- Side effects on the **GitHub API** (github_client posts Check Runs, PR reviews)
- Side effects on the **Anthropic API** (claude_client calls Messages)
- Side effects on **Postgres** (db.writers commits transactions)

It does **not**:

- Push to RQ / Redis (that's worker)
- Receive HTTP webhooks (that's api)
- Enqueue jobs (that's scheduler)

So `reva.*` is safe to import from any process. Cross-process behavior is
glued together by each process's `main.py`.

## Install

Production:

```bash
pip install ./shared
```

Local development (one venv per process directory, editable install so
edits in `shared/reva/` are visible immediately):

```bash
cd worker
.venv/bin/pip install -e ../shared
```

The `worker/requirements-dev.txt` already has `-e ../shared` so a fresh
`pip install -r requirements-dev.txt` does this for you.

## Adding a module

Anything that the api or scheduler will also need belongs here. Anything
worker-specific (orchestration, RQ task wiring) stays in `worker/worker/`.
When in doubt, lean toward `shared/`: it's easier to move modules out of
`shared/` later than to discover a worker-internal module needs to be
imported by the api after the fact.
