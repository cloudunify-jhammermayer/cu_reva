# /fix — finding fix agent — Design

- **Date:** 2026-07-11
- **Status:** approved (design), not yet planned
- **Context:** feature brainstorm 2026-07-11 (2026 market direction: Copilot
  review→agent loop, Cursor Bugbot Autofix ~35% merged). Joseph's locked
  decisions: the fix is **committed directly to the PR branch**, and the command
  is **off by default** (per-repo opt-in).

## Problem

REVA finds the bug and even suggests the fix, but a human still has to apply it.
For fixes bigger than a one-range suggestion block (multi-line, multi-file),
the loop from "found" to "fixed" stays manual — while REVA already has the exact
machinery to close it: a headless Claude Code CLI running against a local clone.

## Design

### Trigger & gating

- Reply **`/fix`** on one of REVA's inline finding comments
  (`pull_request_review_comment` path — same place `/dismiss`/`/mute` live).
  Trusted users only (owner/member/collaborator). Anything after the command is
  passed to the fixer as operator guidance (`/fix use the ORM method instead`).
- **`RepoConfig.fix_command: bool = False`** — OFF by default; repos opt in via
  `.claude-review.yml`. Off → reply explaining how to enable. This is the one
  command that writes to branches, hence opt-in (Joseph's call).
- Requires the GitHub App permission **Contents: Read & write** (operator
  action, documented in the README App-setup table). A push rejected for
  missing permission → thread reply explaining it + ops event — never silent.

### Job (worker, new `fix_runner.py` + RQ task)

1. Resolve the finding from the replied-to comment (the comment→finding mapping
   the reply/feedback paths already use). PR must be open.
2. Record the PR **head SHA at command time**.
3. Clone/update the repo at that SHA (existing `REVA_REPO_CACHE_DIR` machinery),
   work on a throwaway worktree of the PR branch.
4. Run the headless CLI with a new **`reva-fix` skill** (`prompts/skills/`):
   input = the finding (file, line range, severity, title, description,
   suggestion) + optional operator guidance; contract = fix THIS finding only,
   minimal diff, matching repo style; output = the standard temp-JSON contract
   listing changed files + a one-line fix summary. Model: `REVA_DEFAULT_MODEL`.
   Budget-checked like every paid call; cost recorded (`finding_fix`).
5. **Validation gates** (deterministic, before any push): non-empty diff;
   diff ≤ 150 changed lines and ≤ 5 files; no changes under `.github/`
   (workflow pushes would be rejected without workflow permission anyway — we
   refuse earlier); no changes to files outside the repo worktree.
6. **Race guard**: re-fetch the PR head; if it moved since command time → abort
   with a thread reply ("branch moved — re-run /fix"), no push. Then commit as
   the App bot — message `REVA fix: <finding title>` + thread permalink — and
   push the single commit to the PR branch. **Never force-push.**
7. Thread reply: fix summary + commit link on success; reason on every failure
   path. Every degraded path logs + ops event (`component="fix_command"`).

### Interaction with reviews (intentional)

The push triggers the normal debounce → **incremental delta review of REVA's own
fix commit**. This is a feature, not a loop hazard: REVA independently re-reviews
its fix (the delta is tiny, so cost is small). Loop bound: the fix commit's
webhook comes from the App's own bot — `/fix` can only be triggered by a human
reply, and REVA never replies `/fix` to itself (existing bot-reply ignore).

### Idempotency

One `finding_fix` run per (finding, command comment) — a duplicate webhook
delivery dedups as today; an RQ retry after the push re-checks "does the head
already contain my commit?" and skips to the thread reply.

## Explicitly out of scope (v1)

- Running tests/linters on the fix (repos vary too much; the delta review is the
  verification pass). Revisit after real usage.
- Batch fixes (`/fix-all`), fixes for audit issues, fixes on closed PRs.
- Any auto-trigger. A human types `/fix`, always.

## Testing

- Gating matrix: default-off reply, untrusted user ignored, missing permission →
  reply + ops event, budget exceeded → reply, closed PR → reply.
- Validation gates: oversize diff, too many files, `.github/` touch, empty diff —
  each aborts with reply + ops event, nothing pushed.
- Race guard: head moved → abort + reply; unchanged → push (mocked git/GitHub).
- Retry idempotency: commit already on head → no second push.
- CLI runner mocked (subprocess mock, same as review tests); skill-contract test
  for the new `reva-fix` output format.

## Expected behavior & cost

Per invocation: one headless-CLI session on the default model — expect cost
between a diff review and a comment reply (likely $0.10–0.50 for typical
single-finding fixes; capped by the same daily budget), a couple of minutes
wall-clock, then the delta re-review (~$0.10–0.30 more). Staging gates: App
permission upgrade, one live `/fix` on a staging repo, verify the delta review
fires on the fix commit.
