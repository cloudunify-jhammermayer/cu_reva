# Diff-Review Improvements — Verifier Cost/Accounting, custom_instructions, muted_categories — Design

Date: 2026-07-02
Status: approved (design Q&A with Joseph, 2026-07-02)

## Problem

Three gaps in the PR-review pipeline, identified in the 2026-07-02 diff-review
analysis:

1. **Verifier passes are overpriced and mis-ledgered.** Both verifier passes —
   the second-pass self-critique (`worker/worker/reviewer.py:_verify_findings`)
   and the delta-resolution pass (`worker/worker/runner.py:
   _verify_and_resolve_findings`) — are binary verdicts (≤512 output tokens)
   that run on `ClaudeClient.default_model` (Sonnet 5) with the *whole* cited
   file as input. Their spend is **estimated**, wrongly, three ways: the
   self-critique bills at the *review* model's rates (a deep review ledgers
   Opus rates while the call actually runs on Sonnet); the estimate ignores the
   system prompt + finding header; output is hardcoded at 64 tokens. The
   rolling budget cap is only as honest as this ledger.
2. **`custom_instructions` is dead on the review path.** The
   `.claude-review.yml` field is consumed only by the Messages-API
   `reva/prompt_builder.py` (tickets, replies). PR reviews run through the
   headless CLI (`reva/claude_code_runner.py`), which never sees it. Teams
   write review guidance that is silently ignored, with no error. (First noted
   as a side-finding in the 2026-07-02 learned-memory design.)
3. **Muted categories are only filtered after the fact.**
   `_drop_muted_findings` discards muted findings post-hoc, but the model still
   spends effort finding and writing them — and the `test_coverage` param
   actively *prompts* for `test` findings that a `/mute test` then deletes.

Additionally, the self-critique is effectively **off by default**
(`REVA_VERIFY_HIGH_COST` defaults false; `RepoConfig.verify_findings` defaults
`None` → falls back to that global), so most reviews post major/critical
findings unverified.

## Decisions (design Q&A, 2026-07-02)

1. **Scope**: exactly three items — (a) verifier cost & accounting incl.
   content windowing, (b) `custom_instructions` on the review path, (c)
   `muted_categories` prompt param. The confidence-calibration analysis is
   explicitly deferred (separate task; one SQL query over
   `review_findings` × `review_feedback`).
2. **Verification on by default** once it runs on Haiku. Per-repo
   `verify_findings: false` remains the opt-out; existing bounds (20 calls,
   3 errors, budget pre-flight) unchanged.
3. **Verdict objects, not stateful accounting**: `is_resolved` /
   `is_substantiated` return a small result dataclass carrying the verdict plus
   real usage/cost. Rejected: a mutable `last_usage` attribute on
   `FindingVerifier` (shared object in `WorkerContext`, hidden state).
4. **`custom_instructions` as an optional nonce-fenced skill param** in all
   five PR-review skills. Rejected: appending to `_build_preamble` — the
   preamble is REVA's trusted instruction space; `.claude-review.yml` is
   authored by anyone with repo write access and belongs in fenced data space
   (SECU-6).
5. **`muted_categories` as an optional skill param** telling the model up
   front not to report those categories; `_drop_muted_findings` stays as the
   enforcement backstop. When `test` is muted, the `test_coverage` param is not
   attached at all.

## Changes

### 1. Verifier model — Haiku + real usage accounting

- `reva/config.py`: `VERIFY_MODEL = os.environ.get("REVA_VERIFY_MODEL",
  "claude-haiku-4-5")`.
- `reva/cost.py`: add the Haiku entry to `PRICING` (per MTok: input 1.00,
  output 5.00, cache_read 0.10, cache_write_5m 1.25 — verified against the
  Anthropic pricing reference 2026-07-02). Without it, Haiku silently prices
  at the Sonnet 4.6 fallback.
- `reva/finding_verifier.py`:
  - Constructor gains `model: str | None = None`; both methods pass it to
    `self._claude.review(model=self.model)`. Per-call model, **not** a second
    client — the `ClaudeClient` instance is shared with other worker paths.
  - `is_resolved` / `is_substantiated` return a `VerifierVerdict` dataclass:
    `verdict: bool`, `model: str` (echoed from the API response), token counts
    (input/output/cache_read/cache_creation), and `cost_usd` computed via
    `estimate_cost` from the response's **actual** usage (`_parse_success`
    already extracts it). Fail-safe semantics unchanged: missing tool call →
    keep/unresolved verdict, zero-usage verdict object.
- Call-site updates:
  - `reviewer._verify_findings`: sum `verdict.cost_usd`; delete the
    `estimate_cost(model, len(content) // 4, 64)` line and the review-model
    parameter from the cost math.
  - `runner._verify_and_resolve_findings`: same — delete the
    `verify_model = ctx.claude.default_model` estimate; ledger actual cost
    (spend category `"delta_verify"` unchanged).
- Wiring: `worker/worker/settings.py` gains `verify_model: str` (env
  `REVA_VERIFY_MODEL`, default `claude-haiku-4-5`); `runner.py` constructs
  `FindingVerifier(claude=claude, model=settings.verify_model)`.

### 2. Content windowing for verifier input

- New helper in `reva/finding_verifier.py` (shared by both methods):
  when the finding has a `line_start`, send only ±150 lines
  (`_VERIFY_CONTEXT_LINES = 150`) around it; whole file when `line_start` is
  null or the file fits inside the window.
- The excerpt keeps its place: a REVA-authored label **outside** the nonce
  fence — `Excerpt: lines {a}–{b} of {path} ({total} lines total)` — precedes
  the fenced content, so the verifier knows it is partial and where it sits.
- Both system prompts note the content may be an excerpt around the cited
  location. The conservative defaults stand: cannot locate the code →
  `resolved=false` / `substantiated=true` (keep).

### 3. Verification on by default

- `worker/worker/settings.py`: `verify_high_cost` becomes
  `verify_findings_default: bool = True`, read from `REVA_VERIFY_FINDINGS`
  (default `"true"`). Compat shim: when `REVA_VERIFY_FINDINGS` is unset and the
  legacy `REVA_VERIFY_HIGH_COST` **is** set, honor the legacy value and log a
  deprecation warning — no ops change required at deploy.
- `Reviewer` constructor arg `verify_high_cost` renamed accordingly; the
  resolution order is unchanged: `repo_config.verify_findings` (explicit
  true/false wins) → global default.
- Scope/gating unchanged: full/deep verify every file-bearing finding; other
  modes only findings at/above `block_on_severity` (default major);
  `_MAX_VERIFICATIONS = 20`, `_MAX_VERIFY_ERRORS = 3`; the runner's pre-flight
  budget check (`verify_budget_ok`) still gates the pass.

### 4. `custom_instructions` on the review path

- `Reviewer.execute`: when `repo_config.custom_instructions` is non-empty,
  attach it as skill param `custom_instructions`, capped at
  `_CUSTOM_INSTRUCTIONS_MAX_CHARS = 4000` (truncate + log). **Optional param**
  — repos without it keep a byte-identical cached prompt prefix (same rationale
  as `already_reported` / `test_coverage`). The runner nonce-fences every param
  already; no runner change.
- Skill docs: all five PR-review skills (`reva-diff-review`,
  `reva-delta-review`, `reva-full-review`, `reva-xml-review`,
  `reva-migration-review`) gain a short shared section: `custom_instructions`
  is team-authored review guidance from repo config; it adjusts what to
  prioritize and report for this repo; it **never** overrides the severity
  definitions, the security/conduct rules, or the output format, and is data,
  not commands. Audits and ticket paths are excluded (the Messages-API
  `prompt_builder` keeps its existing consumption for tickets/replies).
- `prompts/CHANGELOG.md` version bump.

### 5. `muted_categories` prompt param

- `Reviewer.execute`: fetch `get_muted_categories` **once, early** (currently
  fetched at step 12; reuse the same set for the post-hoc drop). When
  non-empty, attach skill param `muted_categories`: a REVA-rendered sentence —
  "The team muted these finding categories for this repo — do not report
  findings in them: {sorted, comma-separated}."
- When `"test"` is in the muted set, skip `analyze_test_coverage` /
  `test_coverage` attachment entirely (don't prompt for findings we'll
  delete).
- `_drop_muted_findings` stays as the enforcement backstop.
- Skill docs: one short paragraph in the same five skills (shares the new
  section with `custom_instructions`); covered by the same CHANGELOG bump.

### TUI / API

No new surface (CLAUDE.md principle 5 checked): verify outcomes and costs land
in the existing logs and spend ledger; mutes are already visible in the TUI.

## Security notes

- `custom_instructions` is semi-trusted (repo write access ≠ REVA operator):
  it enters only as a nonce-fenced skill param (SECU-6), never the preamble;
  size-capped; the skills state it cannot override severity/security rules or
  the output contract.
- `muted_categories` is REVA-rendered from validated DB values (the `Category`
  Literal set) — fenced like every param regardless.
- Windowing does not change fencing: the excerpt is still nonce-fenced
  untrusted data; only the REVA-authored bounds label sits outside the fence.

## Testing

- **cost.py**: Haiku entry resolves exactly and via dated/vendor-prefixed ids
  (`claude-haiku-4-5-20251001`, `us.anthropic.claude-haiku-4-5`).
- **finding_verifier**: verdict object carries real usage and cost; `model`
  param reaches `claude.review`; fail-safe paths return keep/unresolved with
  zero cost; windowing (line mid-file / near start / near end / `line_start`
  None / file shorter than window); excerpt label bounds correct; nonce
  fencing intact.
- **reviewer**: `_verify_findings` sums verdict costs (estimate line gone);
  gating matrix — repo `None` + global default → enabled, repo `false` →
  disabled, repo `true` + global off → enabled; `custom_instructions`
  attached / absent / truncated-with-log; `muted_categories` attached and
  `test_coverage` suppressed when `test` muted; clean repos produce the
  identical param set as today (prompt-prefix stability).
- **runner**: resolution pass ledgers actual verify cost under
  `"delta_verify"`.
- **settings**: `REVA_VERIFY_FINDINGS` parsing; legacy `REVA_VERIFY_HIGH_COST`
  honored + deprecation logged; `REVA_VERIFY_MODEL` default.
- **prompt files**: update `test_prompt_files` anchors if the new skill
  sections introduce anchored phrases.
- Definition of done per CLAUDE.md: worker + api + scheduler suites green
  (shared `reva/` touched), ruff; no TUI change; no migration (nothing
  Postgres-only to validate).

## Rollout / compatibility

- **Verification default-on**: bounded (≤20 Haiku calls per review, Haiku
  input windowed, budget-gated) — marginal cost per review is cents-level.
  Repos opt out via `verify_findings: false`. Watch
  `findings_verification_done` / `finding_unsubstantiated_dropped` after
  deploy; if drop counts look wrong, flip `REVA_VERIFY_FINDINGS=false` without
  a code change.
- **Legacy env**: `REVA_VERIFY_HIGH_COST` keeps working (with a deprecation
  log) until removed in a later cleanup.
- **`custom_instructions` goes live**: any repo that already configured it has
  been silently ignored until now — its next review starts honoring it.
  Announce to teams before deploy.
- No DB migration, no API schema change, no Odoo-side change.

## Out of scope

- Confidence-calibration analysis (deferred — separate task).
- Hunk-range hints for inline anchoring; cache-hit-rate monitoring.
- PR-wide thread-resolution lookback (own spec, 2026-07-02) and per-repo
  learned memory (own spec, 2026-07-02 — its `team_review_preferences` param
  will reuse exactly the optional-skill-param mechanism established here).
- Any change to oversized-diff handling — the size limits are an intentional
  process guard (PRs too large for human review are declined, not partially
  reviewed).
