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

Worker image gets the pinned `codegraph` npm package (`npm i -g
@colbymchenry/codegraph@0.9.8`). In `claude_code_runner.review()`,
when CodeGraph is enabled **and** the skill is repo-aware:

```
ensure_repo (clone @ head_sha)        # existing, under repo_lock
  └─ codegraph init|sync <repo_path>  # NEW: init (first time) / sync (refresh); in-clone .codegraph/codegraph.db
  └─ write mcp-config.json            # NEW: {"mcpServers":{"codegraph":{"type":"stdio","command":"codegraph","args":["serve","--mcp"]}}}
  └─ claude --print …                 # MODIFIED:
        --mcp-config <mcp-config.json>
        --allowedTools "Read,Grep,Glob,Write,mcp__codegraph__*"
        # + steering note in the skill prompt (see "Steering", below) — REQUIRED
```

**Corrected against the live CLI (spike 2026-06-01):** CodeGraph is an npm
package (not a standalone binary); the MCP server is `codegraph serve --mcp`
(not `codegraph mcp`); `codegraph init` must run before `index`/`sync`. The
server runs in the clone (cwd) and finds `.codegraph/codegraph.db` there; that
dir self-gitignores so it never pollutes the diff.

### Steering (REQUIRED — spike finding)

Adding the tools to `--allowedTools` is **necessary but not sufficient**: in
`--print` mode the model defaults to Grep/Read and never reaches for CodeGraph
on its own (observed twice). The repo-aware review skills must include a short
note pointing the model at the codegraph tools for structural / callers /
where-is questions. Without it the token/tool-call win does not appear.

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

## Spike results (2026-06-01, live in the worker container)

Pinned **0.9.8** (latest; 0.9.5 from the original spec is 3 releases behind).
Target: `OCA/server-tools` (25M, 464 Python files) as a representative
`custom_addons` stand-in. A/B on one structural review task (Sonnet):

| | Baseline (Read/Grep/Glob) | CodeGraph-steered |
|---|---|---|
| Cost | $0.2337 | **$0.1722 (−26%)** |
| Exploration tool calls | ~25 (17 Read, 6 Grep, 2 Glob) | **7** (4 callers, 1 context, 1 explore, 1 files) |
| Index build | — | **4s** → 3,229 nodes / 5,738 edges |

Confirms the value proposition. The MCP wiring works end-to-end (server
`initialize`/`tools/list` handshake OK; the model called `mcp__codegraph__*`
and returned correct graph data). **Decisive caveat: steering is required**
(see above) — without the prompt note the model grepped and the win vanished.

## Open spikes / risks

1. ~~**CLI/MCP contract**~~ — **RESOLVED.** `--mcp-config` stdio format works;
   tool ids are `mcp__codegraph__codegraph_*` (wildcard `mcp__codegraph__*`
   covers them). Corrected commands recorded above.
2. ~~**Odoo language coverage**~~ — **RESOLVED.** 464 Python files fully indexed
   (1,073 methods, 268 classes, 149 functions); XML parsed too.
3. ~~**Indexing latency**~~ — **RESOLVED for typical repos** (4s); per-SHA caching
   stays deferred. Re-evaluate only if a very large `custom_addons` is slow.
4. **Pre-1.0 stability** — still open: pin 0.9.8, flag off until validated on a
   real PR, fall-back covers a bad release.
5. **"pending" init race** — `claude` emits its init snapshot before the MCP
   handshake completes, so first-turn tools may be Grep before codegraph
   registers. Benign in the spike (tools used later in the same session), but
   the steering note + the fact that reviews are multi-turn make this a non-issue
   in practice. Watch for it if a review is unusually short.

## Rollout

1. Spike (above) on a staging worker with the flag on for one repo.
2. If the token/tool-call win is real and reviews are stable, enable for `deep`
   first (highest value, lowest volume), then `full`/`audit`.
3. Keep `diff` off CodeGraph.

## Out of scope (explicit)

Diff-path CodeGraph; the human-facing repo overview/health view (E2); per-SHA
caching; any CodeGraph cloud/SaaS features (we use it purely local).
