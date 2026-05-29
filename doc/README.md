# doc/ — Architecture and Design Documents (LEGACY)

> ⚠️ **Historical reference, not current.** These numbered docs describe REVA's
> *original* design, where reviews ran entirely through the Claude **Messages
> API** against a raw diff. REVA has since moved PR reviews and audits to a
> **headless Claude Code CLI** against a local repo clone (Messages API kept
> only for ticket analysis + comment replies), and added ticket analysis, repo
> audit, delta reviews, and a Vue frontend.
>
> **Authoritative now:** the per-module `README.md` files, the top-level
> [`README.md`](../README.md), and the design specs in
> [`docs/superpowers/`](../docs). Use the docs below for background only.

Originally the authoritative reference for how REVA was meant to work.

## Reading order

| File | What it covers |
|---|---|
| `00-overview.md` | Project goals, decisions, tech stack, repo structure, phases |
| `01-architecture.md` | High-level data flow, container layout, failure modes |
| `02-github-app-setup.md` | Creating and configuring the GitHub App |
| `03-database-schema.md` | Postgres schema, migrations, indexes, analytics queries |
| `04-fastapi-service.md` | Webhook receiver + internal API design |
| `05-queue-and-debounce.md` | Redis + RQ setup, debounce strategy |
| `06-review-worker.md` | Worker implementation, Claude integration, prompt caching, retries |
| `07-claude-prompts.md` | Prompt design, versioning, tool_use contract |
| `08-github-output.md` | Check Run + PR Review + inline comment templates |
| `09-docker-and-deployment.md` | Docker images, Compose, Nginx, TLS, deploy |
| `10-tui.md` | Bubble Tea TUI design and views |
| `11-notifications-and-alerting.md` | Google Chat webhook + alert rules |
| `12-configuration.md` | Global defaults, per-repo `.claude-review.yml`, `CLAUDE.md` |
| `13-security.md` | Secrets, isolation, input validation, cost control |
| `pr-review-requirements.md` | Severity levels, categories, output contract, Odoo 19 rules |

## Document conventions

- Code/SQL/yaml inside fences shows the **target** shape, not necessarily what's already implemented. The HANDOFF.md at the repo root tracks implementation state.
- Names referencing the agent should read **REVA**. Some older sections still say `ARIA`; those are slated for cleanup in the slices that touch them.
- When a doc and the code disagree, the doc loses. Update the doc in the PR that changes behavior.

## Status of each doc vs. implementation

As of HANDOFF.md slice 8:

- ✅ Fully implemented: 03 (schema), 06 (worker logic), 07 (prompts), 08 (GitHub output), 12 (configuration consumption side)
- 🟡 Partially implemented: 01 (architecture — worker side done), 05 (queue retries)
- ❌ Not yet implemented: 02 (GitHub App setup is a deploy concern), 04 (FastAPI), 09 (Docker Compose), 10 (TUI), 11 (notifications), 13 (security ops)
