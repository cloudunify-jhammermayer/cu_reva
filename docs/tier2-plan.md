# Tier 2 — Implementation Plan & Status

The 9 Tier-2 "review intelligence & Odoo depth" features. Each was planned against the code and
adversarially verified (a `plan→verify` workflow); the verdict's corrections are folded into the notes
below. Companion to [`FEATURE_ROADMAP.md`](../FEATURE_ROADMAP.md) and the
[`docs/delta-resolution-analysis.md`](delta-resolution-analysis.md) that motivated features 1–2.

## Status

| # | Feature | Effort | Confidence | Testability | Status |
|---|---|---|---|---|---|
| 1 | Delta-aware finding suppression | S | high | unit | ✅ `0786cc0` |
| 2 | Force-push/rebase delta-base guard | S | high | unit | ✅ `05bbcf9` |
| 3 | Test-coverage gate | M | high | unit | ✅ `a336002` |
| 4 | Intent-grounded review | M | high* | unit | ⬜ ready |
| 5 | `__manifest__.py` validator | M | high | unit | ⬜ ready |
| 6 | Second-pass self-critique | M | high | unit + staging | ⬜ ready |
| 7 | Migration-safety review skill | M | high | unit (routing) + **staging** | ⬜ ready |
| 8 | XML/QWeb review skill | M | high* | unit (filter/routing) + **staging** | ⬜ ready |
| 9 | Security-model consistency | S | medium | mostly **staging** | ⬜ ready |

`*` plan was accurate apart from the corrections noted below. "staging" = the review *quality* (does the
model emit the right finding) can only be validated by the live `claude` CLI on a real Odoo repo — the
plumbing is unit-testable here. **Features 1–3 fix the reported "re-reviews instead of checking old
comments" problem** (1 stops re-emitting duplicates; 2 stops a rebase from producing a garbage delta).

## Done (1–3)

- **Delta-aware suppression** — the delta branch now passes the prior run's open findings as an
  `already_reported` skill param; `reva-delta-review.md` tells the model not to re-post them. Prompt bumped
  to **v1.6**.
- **Force-push guard** — `github_client.get_compare_status` gates the delta path on ancestor status
  (`ahead`/`identical`); a rebase/`diverged` falls back to a full review (and skips the resolution pass).
- **Test-coverage gate** — `diff_utils.analyze_test_coverage` flags modules adding logic with no `tests/`
  change; passed as `test_coverage` to the diff/delta/full skills.

## Remaining (4–9) — plan + corrections to apply

### 4. Intent-grounded review (M, unit-testable)
Parse `closes/fixes/resolves #N` from the PR body, fetch the issue via a **new** `github_client.get_issue`,
pass a nonce-fenced `stated_intent` skill param + guidance to flag diffs that contradict/under-deliver the
stated intent.
- **Corrections:** import `re` + `secrets` in reviewer.py; add `get_issue` to the `GitHubReader` Protocol +
  client + `FakeGitHub`. **Defer** the `ticket_analyses` acceptance-criteria enrichment entirely (the
  ticket↔PR linkage is lossy) — ship the GitHub-issue path only. Default-on, no RepoConfig flag.

### 5. `__manifest__.py` validator (M, unit-testable)
New `reva/odoo_manifest.py` (pure: `parse_manifest`, `audit_manifest` — missing data files, data order
security-before-views, version format) + a `manifest_audit` skill param when a changed module's
`__manifest__.py` is in the diff.
- **Corrections:** the audit goes into `skill_params` (CLI path), **not** `build_user_prompt` (that's the
  legacy Messages-API path). Unit-test `odoo_manifest.py` with a dict-backed `file_exists` fake.

### 6. Second-pass self-critique (M, unit-testable plumbing)
For full/deep reviews, re-verify each major/critical finding against the cited file region before posting;
drop the ones the verifier rejects. Generalize `finding_verifier.py` (`is_resolved` → a sibling
`is_substantiated`), budget-bounded via `budget_exceeded`.
- **Corrections:** `FindingVerifier.is_resolved` returns a `ClaudeResponse` **without token counts** —
  wire cost accounting explicitly (don't assume `estimate_cost` has tokens). The `Reviewer(...)` constructor
  in `build_worker_context` must pass the verifier + a `verify_high_cost` flag.

### 7. Migration-safety review skill (M, routing unit-testable; quality → staging)
`diff_utils.migration_paths()` detects `custom_addons/**/migrations/<ver>/{pre,post,end}-migrate.py`; route
them to a **new** `reva-migration-review.md` skill (path-based override of the mode→skill selection).
- **Notes:** clean layering, no conflicts. Place the `_select_skill` call after the trivial-diff
  short-circuit, before `skill_params` assembly. New skill prompt → another v1.6 entry.

### 8. XML/QWeb review skill (M, largest; quality → staging)
Stop blanket-stripping `custom_addons/**/*.xml` (remove `.xml` from `DEFAULT_EXCLUDE_EXTENSIONS` *under the
addons prefix only* — keep stripping odoo/enterprise XML and `.po/.pot/.md/.rst`); new `reva-xml-review.md`
skill + routing + a per-repo `max_xml_diff_lines/tokens` cap.
- **Corrections:** `skill_params` is at the post-model-selection block (not line 267); compute routing
  **after** the trivial-diff short-circuit; do **not** add `reva-xml-review` to `_CODEGRAPH_SKILLS`. Verify
  nothing else relies on the blanket `.xml` strip (the `excluded_extensions` log line updates automatically).

### 9. Security-model consistency (S, medium; mostly staging)
When a hunk adds a model (`_name =`), the repo-aware skills Read `security/ir.model.access.csv` +
`security/*.xml` in the same module and flag missing ACL/record-rule coverage.
- **Corrections (lowest-confidence plan):** it's **prompt/skill guidance**, not code — drop the contradictory
  "add a floor rule" idea (if a floor *were* added to `_ODOO_SEVERITY_RULES`, its `_RULE_ANCHORS` entry +
  sync-guard test are mandatory). `delta_base_sha` is **not** in `skill_params`; deep uses `reva-full-review`
  (no separate deep skill). Mostly a v1.6 prompt addition to `reva-full-review.md` + `reva-repo-audit.md`.

## Recommended remaining sequence

1. **Structural batch (unit-testable, high confidence):** 4 intent-grounded → 5 manifest validator →
   6 second-pass self-critique. I can build + fully unit-test these here.
2. **Skill batch (needs staging validation):** 7 migration-safety → 8 XML/QWeb → 9 security-model. I'll
   implement the plumbing + prompts and unit-test routing/filtering, but the review *quality* must be
   validated on a real Odoo PR via the live CLI before you trust them (the A1/A2/CodeGraph live-gate pattern).

Each ships as its own commit, tests green (`make test` + `ruff`), with a `/simplify` pass. Prompt/skill
changes accumulate under prompt version **v1.6**.
