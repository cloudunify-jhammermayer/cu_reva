# Headless Claude Code Integration — Design Spec

**Date:** 2026-05-25
**Status:** Approved
**Author:** Joseph Hammermayer

---

## Overview

Replace all PR review Claude calls with a headless Claude Code CLI subprocess, while keeping the direct Messages API for structured/fast paths (ticket analysis, comment replies). Every PR review mode — diff, full, deep — runs against a locally cloned repo so Claude can read connected files, not just the diff.

---

## 1. Architecture

### Dual-client model

| Path | Client | Used for |
|---|---|---|
| API | `ClaudeClient` (existing) | Ticket analysis, comment replies |
| CLI | `ClaudeCodeRunner` (new) | All PR review modes, repo audit |

### What stays unchanged

- `reva/claude_client.py` — direct HTTP client, no changes
- `reva/ticket_analyzer.py` — uses API, no changes
- `reva/prompt_builder.py` — templates stay as-is, still feed the API path
- `Finding`, `ReviewResult`, `_parse_tool_use()` — same schema contract
- Prompt caching on the API path — unchanged

### Routing table

| Trigger | Mode | Client | What Claude sees |
|---|---|---|---|
| PR push / `/review` | `diff` | `ClaudeCodeRunner` | Cloned repo at head SHA + diff as task context |
| PR `/full-review` | `full` | `ClaudeCodeRunner` | Cloned repo at head SHA, full review task |
| PR `/deep-review` | `deep` | `ClaudeCodeRunner` | Cloned repo at head SHA, Opus model flag |
| Admin API / TUI | `audit` | `ClaudeCodeRunner` | Full repo clone, audit task |
| Developer question reply | — | `ClaudeClient` | Finding context + question |
| Odoo ticket | — | `ClaudeClient` | Ticket text, structured schema |

---

## 2. `ClaudeCodeRunner`

### New file: `reva/claude_code_runner.py`

```python
class ClaudeCodeRunner:
    def __init__(self, repo_cache_dir: str, model: str | None = None) -> None: ...

    def review(
        self,
        repo_path: str,       # absolute path to checked-out repo
        task: str,            # review task description
        model: str | None = None,
    ) -> ClaudeResponse: ...

    def ensure_repo(
        self,
        owner: str,
        name: str,
        head_sha: str,
        token: str,
    ) -> str: ...             # returns repo_path, clones or fetches as needed
```

### `ensure_repo()` logic

```
if /repos/{owner}/{name}/ does not exist:
    git clone https://x-access-token:{token}@github.com/{owner}/{name} /repos/{owner}/{name}
else:
    git -C /repos/{owner}/{name} remote set-url origin https://x-access-token:{token}@github.com/{owner}/{name}
    git -C /repos/{owner}/{name} fetch origin
git -C /repos/{owner}/{name} checkout {head_sha}
```

Token is always refreshed before this step (already done in `reviewer.py`). The URL is rebuilt with the fresh token on every fetch.

### `review()` invocation

```
claude --print \
       --output-format json \
       --model {model} \
       --allowedTools Read,Bash,Grep \
       "{task}"
```

- `cwd=repo_path`
- `env={**os.environ, "ANTHROPIC_API_KEY": settings.anthropic_api_key}`
- Stdout parsed as JSON; `tool_use.input` extracted and passed to existing `_parse_tool_use()`
- Stderr / exit codes mapped to `TransientError` / `PermanentError` — same contract as `ClaudeClient`

### Task description format

Short — the skill handles the rest:

```
/reva-diff-review pr_title="Fix login bug" base=main head=feature/fix diff=<diff text>
```

### Structured output

`--output-format json` returns the full response including tool calls as JSON on stdout. REVA extracts `tool_use.input` and passes it to `_parse_tool_use()` — no change to validation or `Finding` schema.

### Changes to `reviewer.py`

- `Reviewer.__init__` gains `runner: ClaudeCodeRunner` alongside `claude: ClaudeClient`
- `execute()` calls `runner.ensure_repo()` then `runner.review()` for all PR modes
- GitHub diff is still fetched (for stale-check and the task description), but Claude reads files directly from the repo on disk
- The `claude: ClaudeClient` parameter remains for comment reply calls

---

## 3. Skills

### Installed into worker image

| Plugin | Location | How installed |
|---|---|---|
| Superpowers | `~/.claude/plugins/superpowers/` | `claude plugins install superpowers` in Dockerfile |
| REVA skills | `~/.claude/plugins/reva/skills/` | `COPY prompts/skills/ /root/.claude/plugins/reva/skills/` |

### REVA skill files (`prompts/skills/`)

| Skill | Purpose |
|---|---|
| `reva-diff-review.md` | PR diff review — reads connected files, calls `submit_review` |
| `reva-full-review.md` | Full PR review — explores repo freely before calling `submit_review` |
| `reva-repo-audit.md` | Full repo audit — produces structured audit report |

### `CLAUDE.md` in customer repos

When `ensure_repo()` clones a repo that has a `CLAUDE.md`, Claude Code picks it up automatically. This replaces the current behaviour in `prompt_builder.py` where REVA manually fetches and injects it as a system block — no code change needed.

### `prompts/` directory after change

```
prompts/
  system.md              ← unchanged (API path)
  odoo19.md              ← unchanged (API path)
  ticket_analysis.md     ← unchanged (API path)
  full_review.md         ← unchanged (API path fallback)
  diff_review.md         ← unchanged (API path fallback)
  CHANGELOG.md           ← version bump when skills ship
  skills/
    reva-diff-review.md  ← new
    reva-full-review.md  ← new
    reva-repo-audit.md   ← new
```

---

## 4. Repo Cache

### Layout

```
/repos/                          ← named Docker volume
  {owner}/
    {repo}/                      ← working tree
```

### Pruning

A new daily scheduler job removes repos not accessed in N days:

- Check `mtime` of `/repos/{owner}/{name}/`
- Delete if older than `REVA_REPO_CACHE_TTL_DAYS`

### New environment variables

| Variable | Default | Description |
|---|---|---|
| `REVA_REPO_CACHE_DIR` | `/repos` | Root path for cloned repos |
| `REVA_REPO_CACHE_TTL_DAYS` | `30` | Days before a repo is pruned |

Both optional with defaults — no breaking change to `settings.py`.

### Docker changes

**`worker/Dockerfile`:**

```dockerfile
RUN apt-get install -y git
RUN npm install -g @anthropic-ai/claude-code
COPY prompts/skills/ /root/.claude/plugins/reva/skills/
RUN claude plugins install superpowers
```

**`docker-compose.yml` and `docker-compose.prod.yml`:**

```yaml
worker:
  volumes:
    - repo_cache:/repos

volumes:
  repo_cache:
```

---

## 5. Repo Audit Job

### New files

**`worker/worker/auditor.py`**

```python
class Auditor:
    def __init__(self, runner: ClaudeCodeRunner, github: GitHubReader, repos: RepoLookup) -> None: ...
    def execute(self, params: AuditJobParams) -> AuditResult: ...
```

`execute()` steps:
1. Resolve `owner/name` from `repository_id`
2. Get fresh installation token
3. `runner.ensure_repo()` at default branch HEAD
4. Run `claude --print` with `/reva-repo-audit` skill
5. Return `AuditResult`

**`reva/types.py` — two new types:**

```python
class AuditJobParams(BaseModel):
    repository_id: int
    installation_id: int
    requested_by: str | None = None

class AuditResult(BaseModel):
    status: Literal["completed", "failed"]
    summary: str
    findings: list[Finding] = []
    model: str = ""
    duration_ms: int = 0
```

### Trigger points

- **API:** `POST /api/v1/repos/{repository_id}/audit` — enqueues RQ job, returns `{"job_id": "..."}`
- **TUI:** `a` keybind on the Repos tab (mirrors `e` for requeue)

### Persistence

New `audit_runs` table in Postgres. One migration file added to `db/migrations/`.

---

## 6. Docs Restructure

### New layout

```
docs/
  setup/
    local.md                  ← from docs/setup-local.md
    production.md             ← from docs/setup-production.md
    github-app.md             ← from doc/02-github-app-setup.md
  architecture/
    overview.md               ← replaces doc/00-overview.md + 01-architecture.md
    review-pipeline.md        ← replaces doc/06-review-worker.md, updated for dual-client
    claude-integration.md     ← NEW: ClaudeClient vs ClaudeCodeRunner
    repo-cache.md             ← NEW: clone/fetch/prune lifecycle
    prompts-and-skills.md     ← replaces doc/07-claude-prompts.md
    database.md               ← from doc/03-database-schema.md
    queue.md                  ← from doc/05-queue-and-debounce.md
    notifications.md          ← from doc/11-notifications-and-alerting.md
    security.md               ← from doc/13-security.md
    tui.md                    ← from doc/10-tui.md
  reference/
    configuration.md          ← from doc/12-configuration.md + new env vars
    error-handling.md         ← error tables from README + doc/08-github-output.md
    pr-review-requirements.md ← unchanged, moved here
  superpowers/
    specs/                    ← existing, unchanged
    plans/                    ← existing, unchanged
```

### `README.md`

- Add dual-client architecture diagram
- Add `audit` row to trigger table
- Add two new env vars to the environment variables table
- Replace inline error tables with link to `docs/reference/error-handling.md`

### `doc/` (old directory)

Kept with a `_deprecated.md` notice pointing to new paths. Removed in a follow-up after new docs are confirmed complete.

---

## 7. What Does Not Change

- All existing tests — `ClaudeClient`, `Reviewer`, `TicketAnalyzer` test suites unchanged
- The `Finding` schema and `_parse_tool_use()` validation
- Prompt templates in `prompts/` (still used for API path)
- RQ job structure — new job types added, existing ones untouched
- The debounce logic in the scheduler
- Nginx, Redis, Postgres configuration
- The weekly report
