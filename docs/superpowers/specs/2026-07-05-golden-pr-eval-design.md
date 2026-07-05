# Golden-PR replay harness — design

**Date:** 2026-07-05
**Status:** Design approved. **⏸ ON HOLD by decision (Joseph, 2026-07-05):**
the review process is changing (core knowledge, prompt v1.9+, strict outputs);
the golden set will be curated from PRs reviewed by the NEW process. Execute
after the five pending plans land and ≥10 new-process PRs have been reviewed.
**Component:** new `eval/` package (bootstrap, replay, scoring, report), golden
YAML files in-repo, spend-ledger integration. No service/API/TUI changes in v1.

## Problem

Every prompt/model/engine change ships on judgment plus one-off manual staging
checks; six such manual gates are currently owed (Tier-2 features 4–9,
CodeGraph, `--add-dir`, retrieval golden set). There is no way to state "this
change raised recall X% at equal false-positive rate." The roadmap rates this
Tier-3 L/H; nothing of it exists.

## Context (verified 2026-07-05)

- `Reviewer.execute(params: JobParams, verify_budget_ok: bool = True) ->
  ReviewResult` (`worker/worker/reviewer.py:250`) is the **pure** pipeline —
  fetch → CLI → parse, no Check Run/PR posting, no run claim (posting lives in
  `runner.run_review`). This is the replay seam; calling it cannot spam GitHub.
- `JobParams` requires `repository_id`, `pull_request_id`, `installation_id`
  (DB/GitHub identities) — replay therefore resolves them from the deployment's
  DB + GitHub App; golden entries are keyed by human-stable coordinates
  (repo full name, PR number, pinned head SHA).
- Findings carry `file/line/category/severity/confidence`; the outcome ledger
  (migration 015) + dismissal feedback give ground-truth candidates.
- Every replay is a paid CLI call; spend must ride the `claude_spend` ledger.

### Locked decisions

1. **Golden set mined from NEW-process reviews only** — the bootstrap filters
   `review_runs` by `--since` date or prompt version; old-process reviews are
   never used as ground truth.
2. Golden set + expectations are **YAML files in-repo** (reviewable/diffable),
   not DB rows.
3. Replay calls `Reviewer.execute` directly — never `run_review` — so nothing
   posts, claims, or notifies.
4. **v1 is an on-demand operator tool** (worker container, `--yes`-gated cost
   confirmation). Deliberately deferred, named in this spec so the TUI-sync
   principle is waived consciously: DB persistence/trends, TUI surface, CI
   integration, scheduled runs.

## Design

### Golden entries — `eval/golden/<owner>__<repo>__pr<N>.yml`

```yaml
repo: acme/odoo-modules
pr_number: 214
head_sha: 3f9c2ab7e5d01c9a48b7f1e2d3c4b5a69788e0f1   # pinned; replay fails loudly if gone
review_mode: diff
note: sale discount rework — good ACL + compute coverage
expected:            # findings a good review MUST produce
  - file: custom_addons/sale_discount/models/sale_order.py
    line: 87
    line_tolerance: 5          # default 5; omit to use default
    category: security         # omit to match any category at the location
    note: sudo() bypasses record rules
forbidden:           # known false positives a good review MUST NOT produce
  - file: custom_addons/sale_discount/models/sale_order.py
    line: 40
    category: maintainability
    note: dismissed 2026-07-12 — the _inherit extension pattern is fine
```

### Commands (run inside the worker container)

1. **`python -m eval.bootstrap --since 2026-07-15 [--repo owner/name] [--min-findings 1]`**
   Mines completed `review_runs` after the cutoff: findings with outcome
   `resolved_by_fix` → `expected` candidates; dismissed → `forbidden`
   candidates; writes draft YAMLs (marked `draft: true`) for one-time human
   curation. Never overwrites a non-draft file.
2. **`python -m eval.replay --label <name> [--only <file.yml>…] [--model <m>] [--yes]`**
   For each golden entry: resolve `repository_id`/`pull_request_id` from the
   DB and `installation_id` from the GitHub App (missing → loud per-entry
   failure, run continues, entry marked `error` in results); build `JobParams`
   with the pinned SHA; call `Reviewer.execute`; write
   `eval/results/<label>/<entry>.json` (raw findings + usage + cost). Prints
   the cost estimate up front and refuses without `--yes`. Spend recorded as
   `claude_spend(kind="eval")`.
3. **`python -m eval.report --label <name> [--compare <other-label>]`**
   Scores results against expectations; prints + writes
   `eval/results/<label>/report.md`.

### Scoring

- A produced finding **matches** an `expected` entry when: same file, `line`
  within `line_tolerance`, and category equal (when the expectation names one).
  One-to-one matching (greedy by distance) — one finding can't satisfy two
  expectations.
- **Metrics:** recall = matched expected / total expected; precision = matched
  produced / total produced; F1; broken out overall and per category and per
  severity. `forbidden_hits` = produced findings matching a `forbidden` entry
  (the false-positive regression signal) reported separately and prominently.
- `--compare` renders both labels side by side with deltas — the A/B mechanism
  (prompt vX vs vY, model A vs B, CodeGraph/core-knowledge on vs off via env).

### Error handling

| Case | Behavior |
|---|---|
| Pinned SHA no longer fetchable | entry marked `error` in results, listed in report; run continues |
| Repo/PR missing from the deployment DB | same — with a hint naming the missing identity |
| `Reviewer.execute` raises Transient/Permanent | entry `error` with the message; never retried (evals are manual) |
| Budget cap reached mid-run | remaining entries `skipped_budget` (execute's own gate); report says so |
| Draft YAML (`draft: true`) present | replay refuses the entry — curation is mandatory |

### Testing

Scoring/matching is pure and fully unit-tested (fixtures of findings vs
expectations, tolerance edges, one-to-one greedy matching, forbidden hits,
per-category rollups). Bootstrap tested against a seeded SQLite DB. Replay
orchestration tested with a fake Reviewer (no paid calls). The paid path is
exercised only by the real (deferred) first run.

## Activation checklist (when taken off hold)

1. Five pending plans landed; prompt CHANGELOG ≥ the core-knowledge version.
2. ≥10 new-process PR reviews exist across ≥2 repos.
3. Run bootstrap → curate drafts (remove `draft: true`) → commit golden set.
4. First replay = the new baseline label (e.g. `--label v1.9-baseline`).
