# CodeGraph engine layer — design spec (Phase-2 E)

**Status:** draft for review. **Date:** 2026-05-31.
**Goal:** give REVA's headless Claude Code agent a pre-indexed code knowledge graph
(via MCP) on the **repo-aware review modes**, so full/deep reviews and repo audits
are cheaper (fewer tokens/tool calls) and reason better across files.

CodeGraph = a local, MCP-native code knowledge graph (tree-sitter → SQLite),
built for AI coding agents incl. Claude Code. 100% local, no API key, no network.

## Scope (decided)

- **In:** `full`, `deep`, and `audit` runs (repo-aware).
- **Out:** the `diff` path (cost-sensitive, doesn't need deep traversal); the human
  "repo overview" (E2); per-SHA index caching (later if indexing is too slow).
- **Rollout:** behind `REVA_CODEGRAPH_ENABLED` (default **off**), CodeGraph version
  **pinned**, validated against the live CLI/MCP before switching on.

## Why it fits / composes with A1+A2

- CodeGraph's MCP server is a **local stdio subprocess** — no network → fine under
  the A2 egress lock.
- Its index is written **inside the clone (cwd)** → fine under the A1 workspace boundary.
- It only adds **read-only graph-query tools** (`mcp__codegraph__*`) to the allowlist —
  no new write/exec capability.

## Architecture & flow

Worker image gets the pinned `codegraph` binary. In `claude_code_runner.review()`,
when CodeGraph is enabled **and** the skill is repo-aware:

```
ensure_repo (clone @ head_sha)        # existing, under repo_lock
  └─ codegraph index <repo_path>      # NEW: build/refresh the graph (in-clone SQLite)
  └─ write mcp-config.json            # NEW: stdio server = `codegraph mcp --db <…>`
  └─ claude --print …                 # MODIFIED:
        --mcp-config <mcp-config.json>
        --allowedTools "Read,Grep,Glob,Write,mcp__codegraph__*"
```

All of this stays inside the existing per-repo `repo_lock` (the index + clone are
the shared working tree). Everything else (output capture in-cwd, no
skip-permissions) is unchanged from A1.

## Components

| Piece | Change |
|---|---|
| `worker/Dockerfile` | Install pinned `codegraph` binary. |
| `worker/settings.py` + `reva` | `REVA_CODEGRAPH_ENABLED` (bool, default false), `REVA_CODEGRAPH_VERSION` (pin), maybe `REVA_CODEGRAPH_INDEX_TIMEOUT`. |
| `reva/claude_code_runner.py` | When enabled + repo-aware skill: run `codegraph index` (bounded by a timeout, like `_run_git`), generate the MCP config, add the mcp flags + `mcp__codegraph__*` to `--allowedTools`. New helper `_codegraph_prepare(repo_path) -> mcp_config_path | None`. |
| `worker/reviewer.py` | Passes whether the run is repo-aware (it already knows `skill`/mode); no behaviour change beyond that. |

## Failure handling (decided: fall back)

If `codegraph index` or MCP setup fails (non-zero, timeout, binary missing), **log
a warning and run the review without CodeGraph** — never fail or degrade a review
because the optional accelerator hiccuped. A transient index failure is not a
`TransientError` for the job; it's swallowed into the fallback.

## Testing

- **Unit (here, with fakes/mocks):**
  - enabled + `full`/`deep`/`audit` → argv includes `--mcp-config` and
    `mcp__codegraph__*` in `--allowedTools`; index step invoked.
  - `diff` mode → CodeGraph NOT engaged (no mcp flags).
  - `REVA_CODEGRAPH_ENABLED=false` → no mcp flags (current behaviour).
  - index failure → falls back to a normal review (no mcp flags), review still completes.
- **Live validation (spike, in the container — can't be done in CI):**
  - exact `codegraph index` + `codegraph mcp` invocation and the `--mcp-config`
    schema for the installed `claude` version;
  - a real full/deep review completes and the model actually calls
    `mcp__codegraph__*`; measure token/tool-call delta vs off;
  - indexing latency on a representative `custom_addons` repo.

## Open spikes / risks (resolve before enabling)

1. **CLI/MCP contract** — confirm the `claude` CLI's `--mcp-config` format and that
   `mcp__codegraph__*` tool names match what the server exposes. (Same "verify
   against the live CLI" gate as A1/A2.)
2. **Odoo language coverage** — CodeGraph is tree-sitter based (19+ langs); confirm
   it indexes Odoo Python well (XML views less important for code review).
3. **Indexing latency** — if per-run indexing is too slow on large `custom_addons`,
   add per-(repo, head_sha) index caching in the repo cache (deferred for now).
4. **Pre-1.0 stability** — pin the version; keep the feature flag off until the
   spike passes; the fall-back means a bad release degrades gracefully.

## Rollout

1. Spike (above) on a staging worker with the flag on for one repo.
2. If the token/tool-call win is real and reviews are stable, enable for `deep`
   first (highest value, lowest volume), then `full`/`audit`.
3. Keep `diff` off CodeGraph.

## Out of scope (explicit)

Diff-path CodeGraph; the human-facing repo overview/health view (E2); per-SHA
caching; any CodeGraph cloud/SaaS features (we use it purely local).
