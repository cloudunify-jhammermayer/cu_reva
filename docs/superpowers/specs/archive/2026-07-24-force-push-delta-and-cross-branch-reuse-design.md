# Force-push-aware delta + cross-branch review reuse — Design

- **Date:** 2026-07-24
- **Status:** implemented + committed 2026-07-24 (v1 = `diff_hash` only). Unit +
  real-git (`two_tree_diff`) + real-Postgres-migration tested; **not yet live-validated**
  against a real PR / Claude CLI. `content_hash`, cross-branch partial delta, and
  core-version gating remain Future work. (Archived — this location is authoritative.)
- **Repos:** cu_reva only (no Odoo contract touched; no ast-odoo companion change)
- **Context:** requested by Joseph 2026-07-24 after Aurium-Systems#109 was reviewed
  four times. Root cause: the developer iterated a single "promote to prod" commit via
  four amend/force-pushes over ~5 h. Each new head SHA is a fresh
  `(repo, pr, head_sha, review_mode)` tuple (`db/migrations/001_initial.sql:89`), so each
  got its own review — and because every push *diverged* from the previously-reviewed
  SHA, the existing delta path (`worker/worker/reviewer.py:353-398`) bailed to a **full**
  review of the entire ~911-line added module every time. The same module content had
  already passed review on the dev (#101) and stage (#102) PRs.
- **Cross-check:** a Fable 5 adversarial review (2026-07-24) found three blockers in the
  first draft — carry-forward laundering findings via delta runs, a false git-object
  availability invariant, and a second-lock timeout hazard — plus a strong case to cut
  `content_hash` from v1. All are folded into this version; the resolutions are called out
  inline where they shaped a decision.

## Problem

Two distinct wastes, one shared root:

1. **Force-push defeats delta.** Delta today requires the prior reviewed head to be a git
   **ancestor** of the new head (`get_compare_status` returns `ahead`/`identical`,
   `reviewer.py:371`). Any amend / rebase / squash makes the prior head a non-ancestor
   (`diverged`), so REVA re-reviews the whole PR diff from scratch. The common
   "iterate-and-force-push a single commit" workflow therefore pays full price on every
   push.

2. **Promotions re-review identical content.** dev → stage → prod promotion PRs carry
   byte-identical addon content that REVA already reviewed on the source branch, but the
   prod PR is a different `pull_request_id` on a different base, so nothing is reused.

Shared root: REVA's only notion of "already reviewed" is the exact
`(repo, pr, head_sha, review_mode)` tuple. Both features relax *what* counts as a valid
delta base / prior review while keeping the existing posting and finding machinery.

## Decisions (locked)

- **#2 divergent delta — local two-tree diff, gated on base-unchanged.** On divergence,
  compute the true incremental change with a local `git diff <prior_sha> <new_head>` in
  the worker clone, **only when the PR's merge-base with its target branch is identical**
  between prior and new SHA (pure amend/reword — the #109 case). If the base moved (rebase
  onto a newer target) or the prior SHA's objects are gone, keep today's full-review
  fallback.
- **#3 match key — `diff_hash` only, in v1.** Fingerprint every *full-scope* completed
  review with a `diff_hash` (SHA-256 of the reviewed filtered diff, `index` lines
  stripped). Match a new PR's first review against prior full-scope completed reviews in
  the same repo by `diff_hash`. **`content_hash` (subtree-tree-oid matching) is deferred**
  to [Future work](#future-work) — see the rationale below.
- **#3 on match — carry forward the matched PR's open findings.** Post a fresh Check Run +
  PR review on the new PR that reuses the matched **PR's** open, non-dismissed, non-muted
  findings and a recomputed risk, with a pointer note, **without calling Claude**.
- **#2 default-on** (a scope refinement of an existing path — strictly cheaper than the
  full review it replaces). **#3 default-on** behind a global kill-switch, with a per-repo
  opt-out.

### Why `content_hash` and the cross-branch *partial* delta are deferred (not in v1)

The brainstorm chose "diff-hash fast, subtree-hash fallback" and "cross-branch delta". The
cross-check showed both extras carry disproportionate cost for a case #109 does not need:

- **`content_hash` forces git before the scope decision on *every* review** (it hashes
  `git rev-parse <head>:<addon>` subtree oids), whereas `diff_hash` is computed from the
  filtered-diff string already in hand — no git, no lock, no timeout pressure. `diff_hash`
  alone matches the motivating case exactly: a pure module *addition* has a base-independent
  diff (`@@ -0,0 +1,N @@` + full `+` content), byte-identical across dev/stage/prod.
- **Cross-branch *partial* delta has no reliable base.** Unlike #2 (same PR, real
  ancestry), a cross-branch base can be an arbitrarily stale sibling-branch version, so a
  two-tree diff against it can be large and misleading rather than "cheap". Making it safe
  needs a diff-size guard plus a queryable per-addon table — real machinery for a
  non-#109 case.

So v1 is: **#2 (real partial delta, same PR) + #3 `diff_hash` identity carry-forward.**
`content_hash` matching, cross-branch partial delta, and core-version gating travel
together in [Future work](#future-work) (they share the per-addon table). If spec review
wants `content_hash` in v1, it comes back with the finding-set and NULL-policy fixes noted
there — say so and it goes in.

## Part 1 — #2: force-push-aware delta (same PR)

**Where:** `Reviewer.execute`, the delta-detection block `reviewer.py:353-398`.

Today:

```python
status = self.github.get_compare_status(token, owner, name, last["head_sha"], head_sha)
if status in ("ahead", "identical"):
    use_delta = True          # API two-dot compare diff is a true delta
else:
    log.info("review_delta_diverged", ...)   # → falls through to full review
```

New behavior for the `diverged` / `behind` branch (the `ahead`/`identical` branch is
**unchanged** — the API diff is already correct there):

1. **Object presence check.** Before anything, `git cat-file -e <prior_sha>^{commit}` (and
   ensure `new_head` is present — see *git access* below). If either object is missing
   (cache eviction, corrupt-reclone `claude_code_runner.py:212-214`, or a force-pushed-away
   SHA GitHub no longer serves), **full-review fallback** + `record_ops_event`. There is no
   assumption that a missing SHA can be re-fetched.
2. **Base-unchanged gate.** Compute `mb_prior = merge-base(<target_branch>, prior_sha)` and
   `mb_new = merge-base(<target_branch>, new_head)`, where `<target_branch>` is
   `origin/<base_ref>` from `get_pull_request` (`reviewer.py:327`). If `mb_prior != mb_new`,
   the base moved → **full-review fallback** + `record_ops_event(component="reviewer",
   event="delta_base_moved", severity="info", {pr, prior, new})`.
   - This is the right gate: GitHub's PR diff base *is* `merge-base(base, head)`, so
     merge-base equality means the full-diff base is unchanged and the prior review plus
     the two-tree delta together cover exactly the new full diff (the `behind` case and
     target advancement during amends included). Safe-direction edge cases: criss-cross
     histories (multiple merge-bases; `git merge-base` picks one) can differ spuriously →
     a needless full review, never a wrong delta. A cherry-pick pulled from the target
     without moving the merge-base shows up *in* the two-tree diff — correct to review.
3. **Two-tree delta.** If the gate passes: `raw = git diff <prior_sha> <new_head>`
   restricted to `review_prefixes` (`reviewer.py:352`), then `filter_diff`. Set
   `delta_base_sha = prior_sha`; carry prior open findings for context exactly as the
   current delta path does (`get_prior_open_findings`, `reviewer.py:394`).
4. **Empty delta ≠ silent skip.** If the filtered two-tree diff is empty (the amend touched
   only non-reviewed paths / the commit message), do **not** take today's `stale` path: it
   posts a `conclusion="skipped"` Check Run on the *new* SHA (`runner.py:525-533`), which
   would clear a blocking `block_on_severity` gate that the prior full review had failed.
   Instead **same-PR carry-forward**: re-post this PR's own open findings + the prior run's
   risk/conclusion onto the new SHA, via the shared carry-forward builder (Part 2). Content
   is identical in reviewed paths, so inline anchors resolve. (The pre-existing
   `ahead`/`identical` empty-delta `stale` behavior is out of scope and unchanged.)

**Why the clone, not the API.** GitHub's compare endpoint is always merge-base-relative,
so it cannot express a two-tree diff between diverged commits. The worker clone is a
full-history partial clone, so when the objects are present the diff is a pure local read;
this removes the compare API from the divergent path.

**Git access — lock-free, authenticated, degrade-in-place.** All Part 1 git ops
(`cat-file`, `merge-base`, `git diff <a> <b>`) are **read-only object reads** — they never
`checkout`/`reset` the working tree — so they run **without** the per-repo working-tree
lock (`claude_code_runner.py:138-153`) and add nothing to the `REVIEW_JOB_TIMEOUT` budget
(`claude_code_runner.py:86`); concurrent object reads/writes are git-safe against another
job's reset/clean. New helpers on `ClaudeCodeRunner`, **all taking `token`** (the blobless
clone's remote is token-less, so the lazy promisor blob fetch that `git diff` triggers
needs the `http.extraHeader` auth — `claude_code_runner.py:245-252` — without it every
private-repo diff fails):

- `object_present(sha: str) -> bool` — `git cat-file -e <sha>^{commit}`.
- `merge_base(token, ref: str, sha: str) -> str | None`.
- `two_tree_diff(token, a: str, b: str, prefixes: tuple[str, ...]) -> str` — `git diff a b`,
  path-filtered.

`new_head` is materialized by a targeted authenticated fetch of the PR head ref
(`refs/pull/<pr>/head`, reliably served) before these ops; `prior_sha` is relied on from a
prior review's fetch and only presence-checked (step 1), never assumed re-fetchable. Every
Part 1 git failure is **caught at the call site and degraded to a full review** with an
ops event — never re-raised as `TransientError` (a missing object is permanent; re-raising
would burn RQ's 3 paid retries on a job that cannot succeed differently).

## Part 2 — #3: cross-branch reuse (promotions)

### `diff_hash` (persisted on every full-scope completed review)

`diff_hash` = `sha256` (hex) of the filtered diff computed **right after `filter_diff` +
the empty-check** (`reviewer.py:~410`) — before `skip_paths` and the size guards — with
`git`/API `index <old>..<new>` lines removed from the hash input (their abbreviation width
is repo-state-dependent, so stripping them makes the fast path robust; `\ No newline` and
rename markers are kept). Computing it here (not post-`skip_paths`) means store-point ==
lookup-point on both PRs of a promotion, and — crucially — the lookup runs *before* the
size-guard declines, so a matched carry-forward pre-empts a "diff too large" decline
without any reordering of the existing guards.

`diff_hash` is stored **only on full-scope reviews** — it is `NULL` when
`delta_base_sha is not None` (a delta run's `diff` is an increment in local-`git diff`
format that must never be a match key) and on declined / stale / failed / skipped-trivial
runs (nothing reviewed). To know a run's scope at persist time, `delta_base_sha` is now
carried on `ReviewResult` and written by `record_review_completed` (Part 3).

### Lookup

Runs in `Reviewer.execute` **only on a PR's *first* review** — i.e. when
`get_last_completed_review(this_pr)` is `None` — and only when reuse is enabled (see
[Config](#config)) and the trigger is not explicit. Restricting to the first review (per
the cross-check) means the PR has no existing review threads yet, so carry-forward cannot
double-post onto its own prior comments; it still covers the entire promotion case (a fresh
prod PR).

1. Compute this PR's `diff_hash` (right after `filter_diff`, before the size guards).
2. Query prior runs `WHERE repository_id = ? AND status = 'completed' AND diff_hash = ?
   AND check_run_id IS NOT NULL AND pull_request_id != ?`, newest `completed_at` first.
   (`diff_hash IS NULL` on delta runs excludes them automatically.)
3. No hit → normal review, persisting this run's `diff_hash` for future PRs.

Same `repository_id` only — cross-repo content is a different core/context. Because the
lookup precedes the size guards, the **free** carry-forward runs even when the diff would
otherwise be size-declined (`reviewer.py` size guards / budget gate `runner.py:316-321`
bound *Claude* cost; reuse spends none), so an oversized promotion is still reused rather
than declined.

### Carry-forward (on a match) — shared builder, used by #2 and #3

Build a `completed` `ReviewResult` **without a Claude call**:

- `findings` = **`get_open_findings_for_pr(matched_run.pull_request_id)`** — the matched
  *PR's* open, non-dismissed findings (`writers.py:810`), **not** `get_run_findings` of a
  single (possibly delta) run. This is the fix for the top cross-check blocker: the newest
  completed run on an iterated source PR is usually a delta with ~0 findings, so a per-run
  copy would launder the earlier full review's findings. PR-wide open findings also
  inherit the dismissed/outcome filtering for free.
- Then apply the **current** repo governance to the carried set: `_drop_muted_findings`
  (`reviewer.py:776`) against *this* repo's mutes, and `_recompute_risk_level` from the
  surviving findings — so a category muted since the source review is not re-posted, and
  risk reflects what actually carries.
- `summary` = a note: *"Content matches already-reviewed #<pr> (run #<id>); verdict carried
  forward. Reply `/review` to force a fresh review."*
- `block_on_severity` = **this** repo's config value (the `ReviewResult` transient defaults
  to `"major"`, `types.py:227`; leaving the default could ignore a repo's gate setting).
- `diff` = this PR's final filtered diff (the post path parses hunks from it,
  `runner.py:605-606`; unmappable findings fold into the review body — no 422). For a
  `diff_hash` match the diffs are byte-identical, so anchors resolve on the new head SHA.
- `model=None`, all token/cost fields 0; `delta_base_sha=None`; its own `diff_hash` is
  still persisted (so a carried run is itself matchable), and `carried_from_run_id =
  matched_run_id` (Part 3). A carried run records one hop, not the transitive origin.

The normal post path (`runner.py:460`) then creates a fresh Check Run + PR review on the
new PR. `record_ops_event(component="reviewer", event="review_carried_forward",
severity="info", {pr, matched_pr, matched_run_id})` makes reuse visible.

### Escape hatch

`/review` / `/full-review` / `/deep-review` comments and `manual_requeue` set an explicit
`trigger_event` that already bypasses every dedup guard (`poller.py:97`, `runner.py:280`).
Carry-forward checks the same set: **explicit triggers never carry forward** — they always
run a real review. That is the developer's override when prod context differs from the
reviewed branch.

## Part 3 — data model + plumbing (migration 042)

```sql
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS diff_hash           TEXT;
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS delta_base_sha      TEXT;   -- scope: NULL = full-scope review
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS carried_from_run_id BIGINT; -- NULL = a real review
CREATE INDEX IF NOT EXISTS ix_review_runs_diff_hash ON review_runs (repository_id, diff_hash) WHERE diff_hash IS NOT NULL;
```

- `delta_base_sha` — persisted so the fingerprint layer can tell full-scope runs from delta
  runs (it exists today only on the transient `ReviewResult`, `types.py:245`, and is never
  written). `diff_hash` is set only when this is `NULL`.
- `carried_from_run_id` — the `review_runs.id` a carried-forward run reused (`NULL` on
  ordinary reviews); first-class source for the degradation-visibility invariant and the
  TUI label, so reuse is not inferred from ops events.

Numbered idempotent file in `db/migrations/`; matching `diff_hash` / `delta_base_sha` /
`carried_from_run_id` columns on the `ReviewRun` ORM model (`reva/db/models.py:142` — tests
build from models). **Plumbing:** `Reviewer.execute` is pure (result object is the only
channel to the DB, `reviewer.py:1-7`), so `ReviewResult` (`reva/types.py`) gains
`diff_hash` and `carried_from_run_id` fields (it already has `delta_base_sha`), and
`record_review_completed` (`writers.py:191`) writes all three. The carried-findings read is
a distinct helper from the existing `get_findings_for_run` (`writers.py:756`, which returns
only id/path/lines); name it clearly (e.g. `get_open_findings_for_pr` already exists and is
the one to use).

## Config

- **#2:** always on. It only changes cases that are full reviews today and is strictly
  cheaper; no flag.
- **#3:** global kill-switch `REVA_CROSS_BRANCH_REUSE` in `reva/config.py` (default
  **on**). Per-repo opt-out via a new `.claude-review.yml` key `cross_branch_reuse: false`
  (`RepoConfig`, default `True`). Reuse runs only when both are enabled.

Both defaults, if off, degrade to exactly today's behavior.

## Known limitation (explicit, v1)

Reuse matches on **diff content**, not surrounding **context** (Odoo core version, sibling
addons, `.claude-review.yml` differences between branches). A promotion where prod runs a
different core version could behave differently despite identical addon code. v1 accepts
this, mitigated by: (a) the carry-forward note tells the developer, (b) `/review` forces a
fresh review, (c) per-repo + global opt-out. Core-version-gated reuse is
[Future work](#future-work).

## Error handling

- Any Part 1/2 git op failing → caught **at the call site**, logged, `record_ops_event`,
  and **degrade to a full review**. Never re-raised (a missing/GC'd object is permanent;
  re-raising as `TransientError` would trigger futile paid RQ retries).
- `prior_sha` object absent / base moved → full-review fallback + `delta_base_moved` (or a
  distinct `delta_object_missing`) ops event.
- Lookup query failure → treated as no-match; the review still happens.
- Carry-forward post failure → the normal post-path error handling applies (it is an
  ordinary `completed` result to that layer).

## TUI (CLAUDE.md principle #5)

Carry-forward is a new review *outcome* that must not be invisible:

- `/api/v1` reviews: add a `carried_from` field (`{run_id, pr}` or null) populated from the
  `carried_from_run_id` column, the pr resolved by joining that run.
- TUI Reviews view: a "carried from #N" label on such runs, matching the existing
  tab/client patterns (`internal/ui/*.go`, `internal/api/{client,iface,mock,types}.go`);
  `go build/vet/test ./...` stays green.

The `delta_base_moved` / `delta_object_missing` / `review_carried_forward` ops events also
show in the existing Failures/ops-events surface.

## Testing

Unit (SQLite + mocked GitHub/httpx + mocked git helpers):

- **#2 base-unchanged gate:** amend (same merge-base) → two-tree delta used, correct
  `delta_base_sha`, prior findings carried; rebase-onto-newer-base (merge-base differs) →
  full-review fallback + `delta_base_moved`; `prior_sha` object missing → full-review
  fallback + ops event (no re-fetch assumed); empty reviewed-path delta → **same-PR
  carry-forward** re-posts the prior conclusion on the new SHA (NOT a `skipped` check).
- **#2 unchanged paths:** `ahead`/`identical` still use the API compare diff (no
  regression); no prior review → full review; git failure never re-raised as
  `TransientError`.
- **#3 `diff_hash`:** stable for a pure addition across differing bases; `index` lines
  excluded from the hash; `NULL` on delta / non-completed runs; store point == lookup point
  under `skip_paths`.
- **#3 lookup + carry-forward:** first-review-only (a PR with a completed run never carries
  forward → no double-post); newest match wins; same-repo-only; current PR excluded; a
  matched **delta** run is never selected (NULL hash) and, if forced, carry uses PR-wide
  open findings not the delta run's; carried findings pass through *current* mutes and
  dismissals; risk recomputed; `block_on_severity` from current config; oversized/over-budget
  diff still carries forward; explicit `/review` / `manual_requeue` never carries forward;
  global + per-repo opt-out each disable reuse; `review_carried_forward` ops event.
- **Carry-forward shape:** `ReviewResult(status="completed")`, 0 tokens/cost, model=None,
  `diff` set, `carried_from_run_id` set, `delta_base_sha=None`.

Real-git integration (SQLite/mock suites cannot exercise real `git diff` / merge-base /
`cat-file`; gate like `make test-integration`): amend / rebase-onto-newer-base /
cross-branch-identical / **evicted-cache-object-missing** fixtures, asserting the two-tree
diff, merge-base decision, and presence-check degradation on real repositories.

**Honest gap:** the partial index in migration 042 is raw SQL — exercised only on real
Postgres (`make test-integration` or first staging boot), not the SQLite suites.

**Suites (definition of done):** `reva/` change → run worker + api + scheduler
(`make test`), plus ruff. `tui/` change → `cd tui && go build ./... && go vet ./...`. No
Odoo contract touched, so no `contracts/` regeneration.

## Future work

- **`content_hash` matching + cross-branch partial delta.** Fingerprint the changed-addon
  subtree tree-oids (base-independent; matches promotions that *modify* files), backed by a
  queryable `review_run_addons(review_run_id, repository_id, addon_path, tree_oid)` table
  for candidate-base selection by shared addon path; on a near (non-identical) match use the
  matched SHA as a delta base and review only the two-tree difference, guarded by a
  diff-size cap (fall back to full review above `max_diff_lines`/`max_diff_tokens`). Carry
  the same finding-set fix (PR-wide open findings, current mutes).
- **Core-version-gated reuse.** Only carry forward when the target Odoo/core version matches
  between the matched review and the new PR (via `odoo_registry` / core-knowledge), closing
  the context-mismatch limitation.

## Out of scope

- Debounce tuning / burst coalescing (a separate lever; not touched here).
- Any change to comment / `manual_requeue` bypass semantics.
- No-op same-PR force-push dedup (subsumed by #2's empty-delta same-PR carry-forward).
- `content_hash`, cross-branch partial delta, and core-version gating (Future work above).
- Any Odoo contract or ast-odoo change.
