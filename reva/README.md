# reva/ — shared library (`reva-shared`)

The single source of truth shared by every process. Installed as an editable
package (`pyproject.toml` is at the repo root); the worker, api, and scheduler
all `pip install -e ..` and import `from reva.X import ...`.

**Nothing in this package owns side effects on the queue** — RQ orchestration
is worker-only. This package holds the types, clients, DB layer, and pure
helpers that more than one process needs.

## Modules

| Module | Role |
|---|---|
| `types.py` | Pydantic models — the schema contract: `Finding`, `ReviewResult`, `JobParams`, `AuditJobParams`/`AuditResult`, `TicketJobParams`, `RepoConfig`, `ClaudeResponse`. |
| `errors.py` | `WorkerError` → `TransientError` (RQ retries) / `PermanentError` (RQ fails). |
| `config.py` | `env_or_file` / `required_env_or_file` — read a setting from an env var or, if `{NAME}_FILE` is set, from that file (Docker-secrets convention). |
| `claude_client.py` | Direct **Messages API** client (httpx). Used for ticket analysis + comment replies. Prompt caching, tool_use parse, status→error mapping. |
| `claude_code_runner.py` | **Headless Claude Code CLI** runner. Manages the repo clone cache, per-repo `flock`, runs `claude --print` under a skill, reads the `submit_review` JSON. Used for all PR reviews + audits. |
| `github_client.py`, `_github_http.py` | GitHub App client (JWT auth, installation-token cache, PR/diff/file reads + Check Run / PR Review / comment writes) and shared status→error mapping. |
| `odoo_client.py` | Odoo JSON-RPC client for ticket field write-back. |
| `review_tool.py`, `ticket_tool.py` | Tool-input JSON schemas derived from the pydantic models. |
| `prompt_builder.py` | Assembles cache-tagged system blocks for the Messages-API paths; exposes the prompt `version`. |
| `review_formatter.py` | Pure formatters: Check Run conclusion matrix, PR review body, inline-comment payloads, decline templates, diff-hunk → inline split. |
| `ticket_formatter.py` | Pure formatting of ticket-analysis results. |
| `diff_utils.py` | Diff filtering (reviewable prefixes / excluded extensions / skip paths), line + token estimates, hunk parsing. |
| `finding_verifier.py` | Asks Claude whether a prior finding is resolved — used by the delta-review resolution pass. |
| `cost.py` | Token-count → USD pricing table + `estimate_cost`. |
| `notifications.py` | Google Chat error-alert formatting + classification. |
| `weekly_report.py` | Builds the weekly summary card from DB aggregates. |
| `db/` | Postgres layer — see [`db/README.md`](db/README.md). |

## Why a shared package

The worker, api, and scheduler are separate containers with separate
lifecycles, but they must agree on the JSON shape, the DB schema, and how to
talk to GitHub/Claude. Putting those in one installable package means there is
exactly one definition of each — no copy-paste drift across services. The
import convention makes ownership obvious: `reva.*` is shared and side-effect-free
on the queue; `worker.*` is the only place RQ jobs and posting live.
