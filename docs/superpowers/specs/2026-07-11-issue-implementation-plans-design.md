# Implementation plans in generated issues — Design

- **Date:** 2026-07-11
- **Status:** approved (design), not yet planned
- **Context:** requested by Joseph 2026-07-11 during the feature brainstorm. REVA
  already has the repo clone machinery and the core-knowledge `/core` worktrees;
  generated issues carry title/body/AC/estimate but no "how to build it".
  Locked decisions: optional per-repo toggle (default OFF) **plus** a global REVA
  setting that disables the feature no matter what repo config says; plans are
  written by the deep model (Opus); clearly marked as AI-generated in the issue.

## Problem

Freelancers/devs pick up REVA-generated issues cold: the issue says *what* and
*acceptance criteria*, but the "where in this codebase, which models/views, what
approach" research is repeated by every implementer. REVA can do that research
once, grounded in the actual code — the same way its deep reviews read the repo.

## Design

### Toggles (two layers, both must allow)

- `RepoConfig.issue_plans: bool = False` (`.claude-review.yml`) — per-repo opt-in.
- `REVA_ISSUE_PLANS_ENABLED` (env, default `true`) — global master switch in
  `reva/config.py`. `false` disables the step everywhere regardless of repo
  config (operator kill switch, Joseph's requirement). Effective =
  env AND repo config. A run skipped by either switch logs at debug — a switch
  doing its job is not a degradation, no ops event.

### Pipeline step (fail-soft, after issue creation)

In `ticket_issue_runner`, **after** the issues are created and persisted and
**after** the `issues-created` Odoo callback is sent (plans must never delay or
endanger the contract flow):

1. Budget check (`budget_exceeded`) — over budget skips with an ops event
   (`issue_plans`, `skipped_budget`), issues stay plain.
2. Clone/update the repo at the default branch head (existing
   `REVA_REPO_CACHE_DIR` machinery; core-knowledge worktrees exposed the same
   way the repo-aware review skills get them; CodeGraph MCP if enabled — this
   is a repo-aware path).
3. **One headless CLI session for the whole run** (`REVA_DEEP_MODEL`, new skill
   `prompts/skills/reva-issue-plans/`): input = the run's created issues
   (number, title, body, AC) + ticket name + parent-epic summary; task = for
   each issue, a concise implementation plan grounded in the code — files/
   models/views/security records to touch, approach, integration points,
   pitfalls (Odoo-version quirks from the existing guidance), suggested order
   when issues depend on each other. Output contract: the temp-JSON pattern,
   `{plans: [{number, plan_markdown}]}`. One session, not per-issue sessions —
   amortizes the clone/context and lets plans cross-reference each other.
4. Per issue: append a marked section to the issue body via the existing
   Issues write permission:

   ```
   <!-- reva:impl-plan -->
   ## 🤖 Implementation plan (AI-generated — verify before following)
   …
   <!-- /reva:impl-plan -->
   ```

   Idempotent: a requeued/resumed run replaces an existing block instead of
   appending a second one (marker regex, same pattern as `/describe`). Issues
   whose numbers the model omits/hallucinates are skipped with a warning log.
5. Cost recorded (`record_claude_spend("issue_plans", …)`) and persisted on the
   run row alongside the planning cost.
6. Any failure in steps 2–4: log + ops event (`component="issue_plans"`), run
   stays `completed`, issues stay plain — identical posture to the board
   projection step.

### What the plan is grounded in

The skill reads the repo clone directly (the addon code) and the provisioned
`/core` worktrees (Odoo/enterprise source) — same trust posture as reviews:
repo file content is the model's working material inside the CLI sandbox;
the *output* is posted to GitHub, so internal paths are redacted from the
rendered plan like all posted text.

### Odoo / contract impact

None. Plans live only in the GitHub issue bodies; callbacks are unchanged
(the body isn't part of any callback ref). The Odoo wizard checkbox variant
was considered and deferred — repo config + global switch won (locked); a
per-request contract field can be added later without breaking anything.

## Explicitly out of scope (v1)

- Per-request (wizard) toggle — see above.
- Plans on the parent epic (it aggregates; the children carry the work).
- Re-planning on issue edit / re-run on demand (a resumed run refreshes blocks,
  but there is no standalone "regenerate plans" trigger yet).
- Any linkage to the `/fix` command or PR reviews.

## Testing

- Toggle matrix: repo off (default), repo on + env off, both on — only the last
  runs the step; no ops events for switch-skips.
- Fail-soft: CLI error / malformed output / GitHub edit failure → ops event,
  run still `completed`, callback already sent (ordering asserted).
- Marker idempotency: resumed run replaces the block; hallucinated issue
  number skipped.
- Skill-contract test for the output JSON; redaction applied to plan text.
- Budget skip → ops event, no CLI call.

## Expected cost & latency

One Opus CLI session per opted-in create-issues run: deep-review territory,
~$1–5 per ticket depending on issue count and repo size, landing ~2–5 minutes
after the issues themselves (consultants see issues immediately; plans appear
shortly after). Effort estimate: ~one story (2–3.5 h AI-assisted) — reuses the
clone/CLI/skill/marker/ops-event machinery end to end. Staging gate: one live
opted-in run; read the plans for grounding quality before enabling more repos.
