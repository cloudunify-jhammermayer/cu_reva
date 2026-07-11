# /describe — PR description generation — Design

- **Date:** 2026-07-11
- **Status:** approved (design), not yet planned
- **Context:** feature brainstorm 2026-07-11 (Qodo's `/describe` is the reference).
  Joseph's locked decision: REVA maintains a **marked section** in the PR
  description and never touches what the developer wrote.

## Problem

PR descriptions are chronically empty or stale. REVA is uniquely positioned to
write them: it already resolves the PR's linked GitHub issues AND the underlying
Odoo ticket (competitors see a Jira title at best).

## Design

1. **Trigger**: `/describe` as an `issue_comment` on a PR, trusted users only
   (owner/member/collaborator — the same gate as `/review`). Immediate, no
   debounce. Handled in the api webhook next to the existing commands; enqueues a
   new RQ job `pr_describe`.
2. **Job** (worker, new `describe_runner.py`, Messages-API path like ticket
   analysis):
   - Inputs: PR title + existing description (outside REVA's block), the diff
     (unfiltered by review scope — a description covers the whole change — but
     token-capped with the existing estimator; oversized → truncate file-by-file,
     noting omissions), linked issues (GraphQL closing refs ∪ body refs, reusing
     the intent-check machinery) with nonce-fenced bodies, and the linked Odoo
     ticket name/URL when `resolve_pr_tickets` finds one.
   - Prompt: new `prompts/pr_describe.md` — output sections: What changed / Why
     (grounded in ticket + issues) / Scope & risk notes. German-or-English input
     tolerated, output English. Tool-schema enforced (same pattern as timesheet
     review).
   - Budget-checked before the call (`budget_exceeded`), cost recorded
     (`record_claude_spend("pr_describe", …)`).
3. **Marked section**: REVA's block is delimited by
   `<!-- reva:describe -->` … `<!-- /reva:describe -->` and **appended** to the
   existing description on first run; re-running `/describe` replaces only the
   block (regex on the markers). The developer's own text above it is never
   modified. PATCH via the existing PR write permission. Internal paths redacted
   from the output as everywhere.
4. **Feedback**: on success, 👍-react to the command comment (cheap ack, no
   noise). On failure, reply on the comment with the reason (budget, size,
   error) + ops event (`component="pr_describe"`).
5. **Kill switch**: `RepoConfig.describe_command: bool = True` — explicit-trigger
   commands are human-bounded cost, so default on (unlike `/fix`).
6. **Idempotency**: webhook delivery dedup already exists; a re-run is safe by
   construction (block replacement).

## Explicitly out of scope

- Auto-describe on PR open (trigger-only in v1; revisit if usage shows demand).
- Mermaid/diagram generation.
- Editing PR titles.

## Testing

- Marker insertion/replacement matrix: empty body, dev-text-only, existing block
  (replaced in place), malformed/half-deleted markers (append a fresh block,
  never touch dev text).
- Command gating: untrusted user ignored; kill switch off → explanatory reply;
  budget exceeded → reply + no call.
- Prompt/tool-schema parse test with mocked Claude; diff token-cap truncation.
- Linked-context assembly: issues fenced; Odoo ticket line present when resolved.

## Expected behavior & cost

One Messages-API call per invocation — comparable to a comment reply / ticket
analysis (cents, not dollars; well under a minute). Staging gate: one live
`/describe` on a linked PR.
