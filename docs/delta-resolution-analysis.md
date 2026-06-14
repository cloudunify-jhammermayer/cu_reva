# Analysis: "REVA re-reviews instead of checking the old comments"

**Symptom (reported):** after a developer fixes the code REVA flagged and pushes, REVA posts a fresh
review instead of marking the old finding's comment thread resolved.

**Scope note:** the report implies REVA *had* posted findings, so a `status='completed'` review with
inline comments already exists. That means the delta path **does** engage (verified below) — this is
not a "delta never ran" case. The problem is in what the delta path does and doesn't do.

## How the follow-up push is supposed to work

On a push to a PR with a prior completed review (`worker/worker/reviewer.py:248-263`):

1. `get_last_completed_review()` returns the prior head SHA (filters `status='completed'`, newest by
   `completed_at` — `reva/db/repo_lookup.py`). Intermediate declined/stale/trivial runs don't break it.
2. The reviewer fetches the **compare diff** `get_compare_diff(prev_head, new_head)` and runs the
   `reva-delta-review` skill, setting `delta_base_sha`.
3. Because `delta_base_sha` is set, `runner._verify_and_resolve_findings()` runs: for each prior open
   finding in a touched file, it asks the verifier "is this fixed now?" and, if yes,
   `resolve_review_thread()` marks the GitHub thread resolved.

So the intended behavior is **review the delta + quietly resolve fixed prior threads**. Two things make
that fall short of expectations.

## Why it looks like "just a new review"

### A. A fresh PR Review per push is by design (not a bug)
REVA never edits the previous review in place — every completed review run posts a **new** PR Review
object with its own body + inline comments on the delta. Thread *resolution* of fixed prior findings is
a separate, quiet GraphQL action (`resolve_review_thread`). So "REVA does a new review" is literally
what happens each push; the real complaint is that **the old comment isn't getting resolved** and/or
**the same issue is re-posted**.

### B. The old thread often isn't resolved — several silent gates
`_verify_and_resolve_findings` (`runner.py:~717-793`) only resolves a prior finding's thread if **all**
of these hold; any miss leaves the thread open:

| Gate | Where | Silently skips when… |
|---|---|---|
| finding has a `github_comment_id` | `get_open_findings_for_pr` filters `github_comment_id IS NOT NULL` (`writers.py`) | the finding was **body-level/unmapped** (never an inline thread), so there's nothing to resolve |
| file is in the compare-diff's touched files | `extract_file_paths(result.diff)` | the fix landed in a different file than the finding cites |
| thread still open | `threads.get(github_comment_id)` | already resolved/collapsed |
| **verifier confirms the fix** | `ctx.verifier.is_resolved()` | the verifier (deliberately conservative, SECU-6) can't confidently confirm — line moved, partial fix, ambiguous context → returns **False** → thread stays open |
| within bounds | `_MAX_DELTA_VERIFICATIONS=20`, abort after 3 consecutive errors | a large PR or a flaky verifier truncates the pass |

The **verifier returning False** (conservatism) and **body-level findings having no thread** are the two
most likely reasons a genuinely-fixed issue's comment stays open.

### C. No delta-aware suppression → the same issue is re-emitted  ⟵ biggest contributor
`reva-delta-review.md:13` tells the model *"findings from earlier reviews are reconciled separately —
focus on what's new"* — **but the prior open findings are never actually passed to the model.**
`skill_params` (`reviewer.py:~267-274`) contains only `pr_title / pr_body / diff / changed_files /
base_branch / head_branch`. So if the issue isn't fully fixed (or Claude re-detects it in the new diff),
the delta review posts a **brand-new inline comment** duplicating the still-open one. To the developer
this is indistinguishable from "it just re-reviewed and ignored the old comment."

This is exactly the roadmap's **Tier 2 "Delta-aware finding suppression"** item (not yet built):
*"Show the delta model its still-open prior findings so it stops re-emitting comments on issues already
flagged."*

### D. Force-push / rebase breaks the delta base  ⟵ if the dev rebased
`get_compare_diff(prev_head, new_head)` (`reviewer.py:250`) has **no ancestor check and no try/catch**.
If the developer rebased/squashed before pushing the fix, `prev_head` is no longer an ancestor of
`new_head`, so the compare diff is garbage or huge — the delta reviews the wrong range, the fixed
finding's file may not appear in touched files, and resolution can't match. "Fix then rebase" is a
common Odoo workflow. This is the roadmap's **Tier 4 "Correct delta base on force-push/rebase"** (not
yet built).

## Root-cause verdict

It's a **cluster of design gaps**, not one bug. Ranked for the reported scenario (prior completed review
exists):

1. **No delta-aware suppression (Tier 2)** — re-emits the same finding as a new comment. *This is what
   most makes it look like "a new review that ignored the old comment."* Highest leverage.
2. **Verifier conservatism / body-level findings** — the old thread isn't auto-resolved even when fixed,
   so it lingers next to the new comment.
3. **Force-push/rebase (Tier 4)** — breaks delta entirely if the dev rebased before pushing.

(The agent's top-ranked "prior review wasn't `completed`" and "backfill never stored `github_comment_id`"
are real *general* failure modes but don't fit this report: REVA had posted comments, and the backfill
endpoint bug was already fixed — HANDOFF + commit `5debceb`.)

## Recommendation

Bring **Tier 2 "delta-aware finding suppression"** forward as the primary fix — it directly stops the
re-emission. Pair it with two smaller, high-value reliability fixes:

- **Suppress + carry forward (Tier 2 core):** before the delta CLI call, load the prior run's open
  findings (`get_open_findings_for_pr` already exists) and pass their `{title, file, line}` as a fenced
  `already_reported` skill param, instructing the model not to re-report an issue that's still in the
  same place; carry the unchanged ones to a single "still open" digest instead of new inline comments.
- **Force-push ancestor guard (small, from Tier 4):** before trusting `get_compare_diff`, verify
  `prev_head` is an ancestor of `new_head` (one compare-status call); if not, fall back to a full diff
  review for that push instead of a garbage delta.
- **Visibility:** the resolution pass already logs `delta_resolution_done` / `finding_resolved` /
  `delta_resolution_no_candidates`. Surfacing why nothing resolved (e.g. "verifier did not confirm")
  would make this debuggable in the field.

## Confirming which gate fires in a specific case

```sql
-- Did a prior review complete? (delta only engages off a 'completed' run)
SELECT id, status, head_sha, completed_at
FROM review_runs WHERE pull_request_id = :pr ORDER BY completed_at DESC LIMIT 3;
```
Then in the worker logs for the fix push, look for: `delta_base=<sha>` (delta engaged),
`delta_resolution_no_candidates` (no inline finding matched — likely body-level or wrong file),
`finding_resolved` (it DID resolve), or no delta line at all (full review — prior wasn't completed, or a
rebase produced an empty/odd compare diff).
