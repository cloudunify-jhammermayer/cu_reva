# REVA — Review & Evaluation Agent

Automatic GitHub PR review platform built on Claude.

REVA listens for `pull_request`, `issue_comment`, and `pull_request_review_comment` webhook events, waits 10 minutes for the developer to settle on a head SHA (debounce), then reviews the change and posts a Check Run + PR Review (with inline comments) back to GitHub. All review data is persisted to Postgres for analytics, cost tracking, and developer feedback loops.

REVA uses **two Claude clients** (see `docs/superpowers/specs/archive/2026-05-25-headless-claude-design.md`):

- **Headless Claude Code CLI** (`reva/claude_code_runner.py`) — runs against a *locally cloned copy of the repo* at the head SHA, so Claude can read connected files, not just the diff. Used for all PR review modes (diff / full / deep) and repo audits. Output is the `submit_review` tool schema written to a temp JSON file.
- **Direct Messages API** (`reva/claude_client.py`) — used for the structured/fast paths: Odoo ticket analysis, Odoo timesheet wording review, and inline-comment reply answers.

> **Doc status:** this README, the per-directory `README.md` files, the guides under `docs/`, and the code are authoritative. `HANDOFF.md` is the current work handoff / resume point.

## What's here

Each directory has its own `README.md` explaining how it works and why.

```
.
├── reva/                 Shared library — types, clients (API + CLI), formatters, db, notifications
├── api/                  FastAPI webhook receiver + internal REST API
├── worker/               RQ worker — review / audit / Odoo / comment-reply jobs
├── scheduler/            Debounce poller + weekly-report scheduler
├── tui/                  Go / Bubble Tea ops dashboard
├── frontend/             Vue 3 web dashboard — RETIRED (decommissioned from the stack; TUI is the dashboard)
├── prompts/              API-path prompt templates + headless-CLI skills (prompts/skills/)
├── db/migrations/        Plain-SQL migrations applied at startup
├── nginx/                Reverse proxy (rate limiting) for production; TLS terminates at the Cloudflare edge
├── scripts/              deploy.sh, backup.sh, restore.sh, fake-webhook.py
├── secrets/              GitHub App private key (gitignored, not committed)
├── docs/                 Setup & operations guides + design specs (docs/superpowers/)
├── docker-compose.yml    Local development stack
└── .env                  Environment secrets (copy from .env.example)
```

## Tech stack

| Layer | Technology |
|---|---|
| Webhook + Internal API | Python / FastAPI |
| Job queue | Redis + RQ |
| PR review / audit engine | Headless **Claude Code CLI** against a local repo clone (Sonnet 5 default, **Opus 4.8** for deep + audits; env-configurable) |
| Ticket analysis / timesheet review / comment replies | Claude **Messages API** (`reva/claude_client.py`) |
| Database | PostgreSQL 16 |
| Dashboard | Go / Bubble Tea TUI (the Vue web frontend is retired) |
| Notifications | Google Chat incoming webhook |
| Container runtime | Docker Compose (nginx + TLS in production) |

## Quick start

```bash
cp .env.example .env          # fill in all values
cp your-app.private-key.pem secrets/github-app-private-key.pem
docker compose up --build
```

The API listens on port 8080. Point your GitHub App webhook at `https://<host>/webhooks/github`.

## GitHub App setup

Required **repository permissions**:

| Permission | Level |
|---|---|
| Contents | Read |
| Pull requests | Read & write |
| Checks | Read & write |
| Issues | Read & write |

Required **webhook events**:

| Event | Purpose |
|---|---|
| `pull_request` | Auto-trigger on push / open / reopen |
| `issue_comment` | `/review`, `/review-all`, `/full-review`, `/deep-review` commands |
| `pull_request_review_comment` | Replies to questions on inline comments; `/dismiss` `/mute` `/unmute` commands |

## Triggering reviews

| Trigger | How | Mode / model |
|---|---|---|
| Automatic | Push to an open PR (`opened`, `synchronize`, `reopened`, `ready_for_review`) | `REVA_DEFAULT_REVIEW_MODE` (default `diff`), Sonnet |
| Manual (diff) | Comment `/review` | diff review (custom_addons only), Sonnet |
| Manual (diff, all paths) | Comment `/review-all` | diff review of **every** changed file, not just custom_addons, Sonnet |
| Manual (full) | Comment `/full-review` | full repo-aware review, Sonnet (+ CodeGraph if enabled) |
| Manual (deep) | Comment `/deep-review` | full repo-aware review, **Opus 4.8** (+ CodeGraph if enabled) |
| Repository audit | TUI Repos tab `a`, or `POST /api/v1/repos/{id}/audit` | whole-repo audit on the default branch, **Opus 4.8** — see [Repository audits](#repository-audits) |
| Requeue failed | Press `e` in the TUI Failures tab | re-runs the original mode |

Automatic triggers have a 10-minute debounce so rapid pushes don't waste API calls. Comment triggers are immediate (no debounce). A command on a PR REVA has never seen (e.g. opened before the app was installed) is handled by fetching that PR from the GitHub API on the fly — no reopen/push needed.

### Inline-comment commands (zero-cost feedback)

Reply to one of REVA's **inline finding comments** with a command (trusted users only — owner/member/collaborator). These are recorded directly, with no paid Claude call:

| Command | Effect |
|---|---|
| `/dismiss` `[reason]` | Record that this finding was wrong/unwanted (negative feedback for REVA's learning loop). |
| `/mute <category>` | Stop posting findings in `<category>` **for this repo** (`bug`, `security`, `style`, …). Omit `<category>` to mute the replied-to finding's category. |
| `/unmute <category>` | Lift a category mute. |

Any other reply is treated as a question and gets a (paid) conversational answer. Muting is reversible and records who muted what; it can hide real issues (e.g. muting `security`), so it's a deliberate per-repo choice — `block_on_severity` still governs whatever remains.

## Comment replies

When a developer replies to one of REVA's inline review comments, REVA automatically answers the question. The reply is generated by Claude with full context about the original finding (file, line range, severity, title, and description) and is posted back as a follow-up comment on the same thread.

Anti-loop: REVA ignores any reply sent by a Bot account, so it never replies to itself.

## What gets reviewed

Only `.py` and other code files under `custom_addons/` or `custom-addons/` are sent to Claude. The following are stripped from the diff before any size check or token count:

- Files outside `custom_addons/` / `custom-addons/` (CI configs, root scripts, OCA modules, etc.)
- `.po` / `.pot` / `.md` / `.rst` files (translation catalogs and docs)

`custom_addons/**/*.xml` (Odoo views/QWeb) **is** reviewed — an XML-only PR routes to the dedicated `reva-xml-review` skill. Third-party `odoo/` / `enterprise/` XML is still always excluded.

If a PR contains no reviewable files after filtering, it is declined with an explanatory message rather than silently skipped.

To review **every** changed path on a repo that has no `custom_addons/` (e.g. a non-Odoo repo), set `review_all_paths: true` in that repo's `.claude-review.yml` — automatic PR reviews then behave like `/review-all` for that repo. `odoo/` and `enterprise/` (and lockfiles / minified assets) are still always excluded.

Per-repo config (`.claude-review.yml`):

| Key | Default | Meaning |
|---|---|---|
| `max_diff_lines` | 2 500 | Decline PRs whose reviewable diff exceeds this. |
| `max_diff_tokens` | 60 000 | Decline if the estimated prompt tokens exceed this. |
| `max_xml_diff_lines` / `max_xml_diff_tokens` | none | Optional stricter cap for XML-only PRs (verbose view dumps). |
| `review_all_paths` | `false` | Review every changed path, not just `custom_addons/`. |
| `skip_paths` | `[]` | Glob patterns to drop from the diff. |
| `block_on_severity` | `major` | Lowest finding severity that fails the Check Run (`none` never blocks). |
| `verify_findings` | global | Per-repo override for the second-pass self-critique (see `REVA_VERIFY_HIGH_COST`). |
| `cross_branch_reuse` | `true` | Allow a promotion PR to reuse an identical prior review's verdict (see below). Global kill switch: `REVA_CROSS_BRANCH_REUSE`. |
| `odoo` / `custom_instructions` | — | Apply the Odoo guidance / inject repo-specific reviewer instructions. |

## Incremental & carried-forward reviews

When a PR already has a completed review, REVA reviews only what changed since it, and quietly resolves inline threads whose findings are now fixed:

- **Clean follow-up push** (the new head is a descendant of the last-reviewed SHA): REVA reviews the GitHub *compare* diff.
- **Force-push / amend / rebase** (the new head *diverged*): REVA reviews a **local two-tree `git diff <prior> <new>`** in the worker clone — gated on the PR's merge-base with its target branch being unchanged. If the base moved (rebased onto a newer target) or the prior commit's objects are gone, it falls back to a full review. This stops a promote-to-prod PR that is amended repeatedly from being fully re-reviewed on every push. If the diverged delta is empty (the amend touched only non-reviewed paths), the prior verdict is re-posted onto the new SHA rather than clearing the Check Run.

**Cross-branch reuse (`cross_branch_reuse`, default on).** On a PR's *first* review, if its reviewable diff is byte-identical (`diff_hash`) to a prior completed review elsewhere in the same repo — the dev→stage→prod promotion case — REVA carries that review's open findings forward as a fresh Check Run + PR review **without a paid Claude run**, noting *"carried forward from #N"*. It respects the current repo's mutes/dismissals and recomputes risk. An explicit `/review` (or admin requeue) always forces a fresh review. Disable globally with `REVA_CROSS_BRANCH_REUSE=false` or per-repo with `cross_branch_reuse: false`.

## Repository audits

A repository audit reviews the **whole repo on the default branch** (not a diff) using the deep model (**Opus 4.8**) and, when enabled, the CodeGraph index (below).

Trigger one from the TUI **Repos** tab (press `a` on a repo) or the API:

```bash
curl -X POST https://<host>/api/v1/repos/<repository_id>/audit
```

Every finding is stored in the `audit_findings` table. **Major** and **critical** findings are additionally opened as GitHub issues — titled `[REVA audit] …` and labelled `reva-audit` (REVA creates the label per repo). Re-runs are deduplicated: a finding whose issue is already open is skipped. Lower-severity findings are stored but not turned into issues.

Read results with `GET /api/v1/audit-findings` (filters: `severity`, `repo`, `limit`) or the TUI **Audits** tab (key `8`).

> Audits need the GitHub App **Issues: Read & write** permission (for the issues + label). Issue creation is best-effort: if it fails (e.g. permission not yet granted), the audit still completes and findings stay available via the API/TUI.

## CodeGraph (optional)

With `REVA_CODEGRAPH_ENABLED=true`, the worker indexes the cloned repo using the `codegraph` binary and exposes a read-only code-graph MCP server (`mcp__codegraph__*`) to the **repo-aware** skills only — full/deep PR reviews and repo audits. Diff/delta reviews don't use it. It is fail-silent: any indexing problem logs a warning (`codegraph_index_skipped` / `_failed`) and the review runs without the graph; success logs `codegraph_index_ready`. Off by default.

## Weekly report

REVA sends a weekly summary to Google Chat every Monday at 08:00 UTC (configurable). The report covers:

- Reviews completed, failed, and stale
- Findings by severity
- Total API cost and average cost per review
- Min / avg / max review duration
- Reviews per author (top 10)
- Top 5 recurring findings across all PRs
- Per-repository breakdown
- Model usage split

**Cron schedule** is controlled by `REVA_REPORT_WEEKDAY` and `REVA_REPORT_HOUR_UTC`. A `weekly_reports` table prevents duplicate sends across scheduler restarts.

**Manual trigger** (does not affect cron schedule):

```bash
curl -X POST "https://<host>/api/v1/admin/weekly-report?days=7"
```

The `days` parameter controls how far back the report looks (default: 7).

## TUI

```bash
cd tui
go run . --demo          # demo mode, no live server needed
REVA_API_URL=http://localhost:8080/api/v1 REVA_API_KEY=<key> go run .
```

`REVA_API_KEY` is sent as `Authorization: Bearer`; set it whenever the API has auth enabled (always in production). See [`tui/README.md`](tui/README.md).

| Tab | Key | Contents |
|---|---|---|
| Dashboard | `1` | 24 h / 7 d stats, finding counts, cost, queue depth, active workers |
| Reviews | `2` | All reviews — filter `/`, cycle status `s`, clear filter `c`, requeue `e`, open PR `o` |
| Findings | `3` | All findings (with repo + PR) — `/` text filter (repo · category · title · file), `o` opens the PR, severity filter `a/c/m/n/i` |
| Failures | `4` | Failed / stale reviews — requeue with `e` · badge shows count |
| Repos | `5` | Registered repos — `/` filter by name, add one with `n` (type `owner/name`), trigger an audit with `a`, open on GitHub with `o` |
| Pending | `6` | Reviews waiting in the debounce queue · badge shows count |
| Tickets | `7` | Odoo ticket analyses, grouped by GitHub repo into foldable sections, **collapsed by default** (analysis-only tickets under "no repo yet") — `enter`/`space` open a group · `z` toggles all · `/` filter · `e` requeue · `o` open in Odoo (or the repo on a header) · `enter` on a row drills into issues |
| Audits | `8` | Repo-audit findings — `enter` views a run's findings, severity filter `a/c/m/n/i`, shows the GitHub issue # |
| Feedback | `9` | Per-(repo, category) learning signals (findings / dismissals / fixes) + active `/mute`s |
| Odoo | `0` | Registered Odoo instances, callback config, per-instance spend, budget, activation, and key rotation |
| Timesheets | `-` | Odoo timesheet wording review runs from `GET /api/v1/timesheet-reviews`, with line counts, rewrite/human counts, callback state, and errors |

Global keys: `1–9` switch tabs · `j`/`k` (or arrows) move · `g`/`G` top/bottom · `Ctrl+D`/`Ctrl+U` half-page · PgUp/PgDn page · `r` refresh · `q` quit. Lists that hit their fetch limit show "showing N of M"; narrow with `/`. The free-flowing panels scroll too — the **Reviews** detail pane with `J`/`K` (or PgUp/PgDn), the **Audits** findings and **Feedback** lists with `j`/`k`/PgUp/PgDn.

## Error notifications

REVA posts a Google Chat alert whenever a review fails due to a server or API error. Set `GOOGLE_CHAT_WEBHOOK_URL` in `.env` to enable; leave it empty to disable.

Notifications fire on `PermanentError` and unexpected exceptions. Transient errors (rate limits, 5xx) are retried silently by RQ and only notify if all retries are exhausted.

### Claude API errors

| Error | Title | What to do |
|---|---|---|
| HTTP 401 | Invalid Anthropic API key | Replace `ANTHROPIC_API_KEY` in `.env`, restart worker |
| HTTP 403 | Permission denied | Key lacks access to this model/feature |
| HTTP 400 (context) | Prompt too large for context window | Lower `max_diff_tokens` or split the PR |
| HTTP 400 (other) | Claude rejected the request | See detail in notification |
| HTTP 404 | Model not found | Model not available on this key |
| HTTP 429 | Rate limit exhausted | Manually requeue once limit resets |
| HTTP 529 | API overloaded | Manually requeue once load drops |
| HTTP 5xx | Server error | Retries exhausted — check status.anthropic.com |
| Timeout | Request timed out | Diff may be too large or API is slow |
| Transport error | Cannot reach Anthropic | Check network / DNS |
| Missing tool_use | Claude did not call the review tool | Prompt/model mismatch |
| Bad tool data | Claude returned malformed review | Schema validation detail in notification |

### GitHub API errors

| Error | Title | What to do |
|---|---|---|
| HTTP 401 | GitHub App authentication failed | Key revoked — download new PEM from App settings |
| HTTP 403 (rate limit) | GitHub rate limit hit | Auto-retried |
| HTTP 403 (forbidden) | Permission denied | App needs `contents: read`, `pull_requests: write`, `checks: write`, `issues: write` |
| HTTP 404 | Resource not found | PR or repo may have been deleted |
| HTTP 422 | Validation error | Duplicate review or invalid inline comment position |
| HTTP 5xx | GitHub server error | Retries exhausted — check githubstatus.com |
| Timeout | GitHub API timed out | Network issue |
| Transport error | Cannot reach GitHub | Check network / DNS |

### Configuration errors

| Error | Title | What to do |
|---|---|---|
| Bad PEM file | Invalid GitHub App private key | File must start with `-----BEGIN RSA PRIVATE KEY-----` |
| DB unreachable | Database connection failed | Check Postgres container health |
| Redis unreachable | Redis connection failed | Check Redis container health |

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `POSTGRES_PASSWORD` | ✅ | — | PostgreSQL password |
| `GITHUB_APP_ID` | ✅ | — | GitHub App numeric ID |
| `GITHUB_WEBHOOK_SECRET` | ✅ | — | HMAC secret set in GitHub App webhook settings |
| `ANTHROPIC_API_KEY` | ✅ | — | Anthropic API key (console.anthropic.com) |
| `REVA_DOMAIN` | prod | — | Public hostname; TLS terminates at the Cloudflare edge (no certs on the server) |
| `REVA_API_KEY` | prod | — | Bearer token protecting `/api/v1/*` (the TUI sends it). Required in production |
| `REVA_REQUIRE_API_KEY` | — | `false` | When `true`, the API refuses to start unless `REVA_API_KEY` is set (prod compose sets this) |
| `GOOGLE_CHAT_WEBHOOK_URL` | — | _(off)_ | Incoming webhook URL for error notifications and weekly report |
| `REVA_DEBOUNCE_SECONDS` | — | `600` | Debounce window in seconds |
| `REVA_DEFAULT_REVIEW_MODE` | — | `diff` | Auto-review mode: `diff`, `diff-all`, `full`, or `deep` |
| `REVA_DEFAULT_MODEL` | — | `claude-sonnet-5` | Model for diff/full reviews, ticket analysis, timesheet review, comment replies |
| `REVA_DEEP_MODEL` | — | `claude-opus-4-8` | Model for `/deep-review` and all repo audits |
| `REVA_CODEGRAPH_ENABLED` | — | `false` | Expose a pre-indexed CodeGraph (MCP) to repo-aware reviews + audits |
| `REVA_CODEGRAPH_INDEX_TIMEOUT` | — | `180` | Seconds bounding the CodeGraph index step |
| `REVA_REPO_CACHE_DIR` | — | `/repos` | Root path where the worker clones repos for the headless CLI |
| `REVA_REPO_CACHE_TTL_DAYS` | — | `30` | Days before an unused cloned repo is pruned |
| `REVA_DAILY_BUDGET_USD` | — | _(off)_ | Rolling 24-hour spend cap; reviews are declined (not run) once trailing spend reaches it. The check is serialized (Postgres advisory lock); residual overshoot is bounded by concurrent workers (≤ one in-flight review each) |
| `REVA_STALE_RUNNING_SECONDS` | — | `2 × job timeout` (3600) | Scheduler reaps `review_runs` stuck in `running` longer than this (worker killed mid-review) and marks them failed |
| `REVA_API_RATE_LIMIT_PER_MINUTE` | — | `0` (off) | Per-client (API key / IP) request cap on `/api/v1` over a rolling minute; per-instance, in addition to nginx's limit |
| `REVA_QUEUE_DEPTH_ALERT` / `REVA_FAILED_JOBS_ALERT` / `REVA_REPO_CACHE_DISK_PCT_ALERT` | — | `50` / `10` / `90` | Scheduler operational-alert thresholds (need `GOOGLE_CHAT_WEBHOOK_URL`) |
| `REVA_SECRET_KEY` | — | — | Fernet key for encrypting per-instance Odoo callback keys at rest; required to use the Odoo-instances feature |
| `REVA_REPORT_WEEKDAY` | — | `0` | Day to send weekly report (0 = Monday, 6 = Sunday) |
| `REVA_REPORT_HOUR_UTC` | — | `8` | Hour (UTC) to send weekly report (0–23) |
| `REVA_TICKET_TEXT_RETENTION_DAYS` | — | `30` | Days before raw customer ticket text is scrubbed from `ticket_analyses` |
| `REVA_LOG_LEVEL` | — | `INFO` | Log level for all services (`DEBUG`/`INFO`/`WARNING`/…) |
| `REVA_LOG_FORMAT` | — | `json` | `json` (structured, prod) or `console` (human-readable, local dev) |

## Log rotation

All containers use Docker's `json-file` driver with automatic rotation:

- Max file size: 10 MB
- Max files kept: 5 (50 MB total per service)

No manual log cleanup required.

## Running the tests

Each Python service has its own venv (all install the shared `reva` package as editable). Python 3.14.

```bash
cd worker && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/        # worker: ~658

cd ../api && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/        # api: ~194

cd ../scheduler && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/        # scheduler: ~28
```

Tests use SQLite in-memory, `httpx` MockTransport, and subprocess mocks for the Claude CLI; no Docker or network required. The Go TUI: `cd tui && go test ./...`.
