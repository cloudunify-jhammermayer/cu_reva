# PR-Wide Open-Thread Lookback for Inline-Comment Resolution — Design

Date: 2026-07-02
Status: draft (diagnosis confirmed by reproduction; one decision assumed, see "Decisions")

## Problem

Developers reply to REVA's inline comments and fix the code, but REVA does not resolve
the threads. Root cause (confirmed by a writers-level reproduction on SQLite):

**The resolution pass's memory is one review run deep.**
`get_open_findings_for_pr` (`reva/db/writers.py:797`) returns posted findings only from
the *single most recent completed run* (`ORDER BY completed_at DESC LIMIT 1` subquery).
Every completed review — including a delta review that found nothing new — becomes the
new "most recent run" and permanently hides all older still-open threads from
`_verify_and_resolve_findings` (`worker/worker/runner.py:740`). Net effect: only
findings fixed in the *first* push after a review ever get resolved; e.g. review posts
F1+F2, push 1 fixes F1 (resolved ✓, run completes), push 2 fixes F2 → the lookback now
only sees the F1-run's (empty) findings → F2's thread stays open forever.

Two secondary defects, same mechanism:

1. **Rebase/amend/force-push skips resolution entirely.** A diverged compare status
   forces a full review, and the pass is gated on `result.delta_base_sha`
   (`runner.py:425`) — rebase-heavy workflows never resolve anything.
2. **Duplicate re-posting.** The same one-run lookback feeds `prior_findings` — the
   "don't re-post what already has an open thread" prompt context
   (`reviewer.py:338` → `repo_lookup.get_prior_open_findings`) — so after an
   intermediate run REVA can re-post an issue that already has an open thread.

## Decisions

- **Lookback becomes PR-wide**: all completed runs of the PR, not the newest one.
- **The pass runs after every completed review, delta or full** — covers the rebase
  case. *(Assumed on recommendation — Joseph was away when asked. Revert by keeping the
  `result.delta_base_sha` gate; everything else in this design stands alone.)*
- Findings a developer explicitly `/dismiss`-ed are excluded from verification
  candidates (no recurring paid calls for findings the team has rejected).
- No schema change. The existing outcome ledger (`review_findings.outcome`, default
  `'open'`) is the open/closed source of truth.

## Changes

### 1. `get_open_findings_for_pr` — PR-wide open findings (`reva/db/writers.py`)

New semantics: return findings across **all completed runs** of the PR where

- `ReviewFinding.github_comment_id IS NOT NULL` (actually posted inline),
- `ReviewFinding.outcome = 'open'` (not already `resolved_by_fix` /
  `still_open_at_merge`),
- no `review_feedback` row with `reaction = 'dismissed'` for the finding
  (`NOT EXISTS` subquery on `review_feedback.review_finding_id`),
- run filter: `ReviewRun.pull_request_id = :pr AND ReviewRun.status = 'completed'`,
  plus the existing `before_run_id` guard (`ReviewRun.id < before_run_id`) so the
  current run's own findings stay excluded.

Ordering: `ReviewFinding.id ASC` (oldest first), so when the resolution pass caps at
`_MAX_DELTA_VERIFICATIONS = 20` the longest-open threads win. Return shape unchanged
(same dict keys) — both callers keep working.

Notes:
- Comment ids are unique per finding row, and `_replace_findings` on a re-review
  replaces the run's finding rows, so the union produces no ghost entries.
- The GitHub-side `threads.get(comment_id)` filter in the pass already drops threads
  that were resolved manually or belong to deleted comments — DB-side `outcome` and
  GitHub-side `isResolved` cross-check each other as today.

### 2. Resolution pass on every completed review (`worker/worker/runner.py`)

- Gate change at `runner.py:425`: call `_verify_and_resolve_findings` whenever
  `result.status == "completed"`, not only when `result.delta_base_sha` is set. The
  pass's inputs are delta-agnostic: candidates = open threads in
  `extract_file_paths(result.diff)`; for a full review that diff is the whole filtered
  PR diff, which is exactly the file set a fix could live in.
- Add a pre-flight budget gate (consistent with the reviewer's self-critique gating):
  if `budget_exceeded(ctx)` is not None, skip the pass with a log line instead of
  spending on verifier calls.
- Keep the existing bounds and accounting: `_MAX_DELTA_VERIFICATIONS = 20`,
  `_MAX_VERIFY_ERRORS = 3`, spend recorded as `"delta_verify"`. Keep the existing log
  event names (`delta_resolution_*`, `finding_resolved`) — they are grepped
  operationally; the reviewer comment notes they now also fire on full reviews.
- Update the stale comment in `reviewer.py:298-302` ("skip the resolution pass, which
  keys off delta_base_sha") — after this change divergence only affects diff scope,
  not resolution.

### 3. Duplicate re-post fix — free ride for the prompt context

`repo_lookup.get_prior_open_findings` delegates to the same writer, so the delta
prompt's "already flagged, don't re-post" list automatically becomes the PR-wide open
set. One addition: cap the list at the **30 newest** findings (slice in
`get_prior_open_findings`, log when truncated) so a long-lived PR can't bloat the
prompt. The resolution pass is *not* capped this way (it has its own cap of 20 per
run, oldest first).

### 4. Retroactive effect — no backfill needed

Currently-orphaned threads still exist as `outcome='open'` findings with comment ids
in the DB. The moment this ships, the next completed review of each PR sees them
again; open threads on merged/closed PRs stay as they are (no reviews run there —
accepted).

## Error handling

Unchanged by design: the pass is best-effort (`get_review_threads` failure → skip;
per-finding errors → `_MAX_VERIFY_ERRORS` abort; a failed `resolve_review_thread`
never records `resolved_by_fix` — the M7 ordering guarantee stays).

## Testing

- **Writers** (`worker/tests/test_db.py`, where the existing
  `get_open_findings_for_pr` tests live): union across runs (the reproduction scenario:
  run 1 posts F, run 2 completes empty, lookback before run 3 still returns F);
  `outcome != 'open'` excluded; dismissed-feedback excluded; `before_run_id` excludes
  the current run; ordering oldest-first.
- **Runner** (`worker/tests/test_runner.py`, existing resolve fixtures at ~line 897):
  finding from run N resolved on the *second* fix push (intermediate empty run in
  between); resolution fires on a full (non-delta) completed review; budget-exceeded
  skips the pass; existing tests for M7 ordering / no-candidates keep passing.
- **Reviewer**: prior_findings cap at 30 with truncation log.
- Definition of done per CLAUDE.md: worker + api + scheduler suites green (shared
  `reva/` touched), ruff; no TUI change (no new data — outcomes are already surfaced).

## Ops verification after deploy

Watch worker logs: `delta_resolution_no_candidates` should mostly disappear on PRs
with open threads; `finding_resolved` counts should rise. The prod log grep that
would have confirmed the failure-mode distribution pre-fix (blocked during
diagnosis):
`docker compose logs worker --since 240h | grep -E 'delta_resolution|finding_resolved|review_delta_diverged'`.

## Out of scope

- Resolving threads on `/dismiss` (separate feature decision).
- Threads orphaned by re-review attempts whose finding rows were replaced
  (`_replace_findings`) — pre-existing, rare, unaffected by this change.
- Any UI/TUI change.
