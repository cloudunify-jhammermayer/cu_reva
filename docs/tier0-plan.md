# Tier 0 — Implementation Plan

The four Tier-0 quick wins from [`FEATURE_ROADMAP.md`](../FEATURE_ROADMAP.md). Each is
standalone (no inter-dependencies), low-risk, and high-payoff. Every plan below was drafted
against the real code and then adversarially verified — the `⚠️ Verification fixes` boxes are
corrections the skeptic pass found and that **must** be applied during implementation.

All line anchors are as of commit `0274098`; reconfirm before editing.

| # | Feature | Effort | Net effect |
|---|---|---|---|
| 1 | [Per-repo severity gating](#1-per-repo-severity-gating) | **S** | Repos choose what severity blocks the Check Run; default unchanged. |
| 2 | [Deterministic Odoo severity calibration](#2-deterministic-odoo-severity-calibration) | **S** | Documented anti-pattern severities are enforced (floored, never downgraded). |
| 3 | [Risk labels on the PR](#3-risk-labels-on-the-pr) | **S** | A single `reva-risk-*` label per PR for at-a-glance triage. |
| 4 | [Trivial-diff short-circuit](#4-trivial-diff-short-circuit) | **M** | No paid Claude call on whitespace/comment/import-only diffs. |

**Decisions you need to make** are consolidated at the [bottom](#decisions-needed); each has a
recommended default so implementation can proceed without blocking.

---

## 1. Per-repo severity gating

**What/why.** `compute_check_conclusion` (`reva/review_formatter.py:59`) is hardwired: any
major/critical → `failure`, minor → `neutral`. Let a repo set the blocking threshold in
`.claude-review.yml`. Default `major` preserves today's behavior exactly.

**Files touched**

- `reva/types.py` — add `BlockSeverity = Literal["critical","major","minor","none"]`; add
  `block_on_severity: BlockSeverity = "major"` to `RepoConfig` (~line 61) and a **transient**
  `block_on_severity: BlockSeverity = "major"` to `ReviewResult` (next to the existing transient
  `diff` field, ~line 138).
- `reva/review_formatter.py` — generalize the `completed` branch of `compute_check_conclusion`
  to read `result.block_on_severity`; add `_SEVERITY_RANK = {"info":0,"minor":1,"major":2,"critical":3}`.
  **Signature unchanged** — the threshold rides on `result`.
- `worker/worker/reviewer.py` — in `execute()`, pass
  `block_on_severity=repo_config.block_on_severity` into the completed `ReviewResult(...)`
  constructor (~lines 327-344). `repo_config` is already in scope (loaded ~line 160).
- `worker/worker/runner.py` — **no edit expected**: the sole production caller is
  `_post_completed_check` at `runner.py:497`; threshold travels on `result`.

**Conclusion mapping** (precise)

```
status != completed → unchanged (stale→skipped, declined→neutral, failed→failure)
threshold == "none"            → success            # never gate
max_finding_rank >= thr_rank   → failure
max_finding_rank >= minor      → neutral            # findings present but below the gate
else                           → success            # info-only or no findings
```

So `major` (default) = today; `critical` = only critical blocks; `minor` = minor+ blocks;
`none` = never blocks.

**Config/schema.** New `RepoConfig` field only. No DB migration — the matching `ReviewResult`
field is transient (`record_review_completed` persists only named columns, `reva/db/writers.py:170-193`).
`RepoConfig` uses `extra='ignore'`, so the new YAML key is forward/backward compatible.

> ⚠️ **Verification fix.** A *typo* in the YAML value (e.g. `block_on_severity: high`) raises a
> pydantic `ValidationError` because `extra='ignore'` does **not** suppress invalid values of
> *known* fields — and `_load_repo_config` (`reviewer.py:~356-370`) currently only catches
> `YAMLError`/non-dict. Add `ValidationError` to that fallback so a bad value degrades to the
> `major` default instead of permanently failing every review on the repo.

**Tests**

- `test_review_formatter.py` — `_result()` gains a `block_on_severity` kwarg; cases per threshold
  (`major`: minor→neutral/major→failure/critical→failure; `critical`: major→neutral/critical→failure;
  `minor`: minor→failure/info→success; `none`: critical→success). Existing tests stay green on default.
- `test_reviewer.py` — `.claude-review.yml` with `block_on_severity: critical` ⇒
  `result.block_on_severity == "critical"`; absent key ⇒ `"major"`; invalid value ⇒ falls back to `"major"`.
- `test_runner.py` — `_completed_result()` gains the kwarg (default `"major"` keeps existing tests green);
  a major-only result with `block_on_severity="critical"` posts conclusion `neutral`.

---

## 2. Deterministic Odoo severity calibration

**What/why.** `prompts/odoo19.md` documents the intended severity for each Odoo anti-pattern, but
the model assigns severity freely, so a critical anti-pattern can land as minor. Add a pure helper
that **floors** (raises, never lowers) severity to the documented minimum for canonical anti-patterns.

**Files touched**

- `worker/worker/reviewer.py` — add module-level `_ODOO_SEVERITY_RULES` (ordered, critical-first)
  near `_SEVERITY_WEIGHT` (`reviewer.py:47-48`); add `_calibrate_odoo_severity(findings) -> list[Finding]`
  after `_drop_thirdparty_findings` (~line 455); insert one call in `execute()` **between** line 306
  (`_drop_thirdparty_findings`) and line 307 (`_cap_findings`) — so it runs **before** `_cap_findings`
  (a floored-up critical won't be dropped) and **before** `_recompute_risk_level` (risk + conclusion
  reflect floored severities).
- `worker/tests/test_reviewer.py`, `worker/tests/test_prompt_files.py` — unit + sync-guard tests.

> ⚠️ **Verification fix.** Add `Callable` to the `typing` import and `Severity` to the
> `reva.types` import block in `reviewer.py` — neither is currently imported there.

**Algorithm.** Reuse `_SEVERITY_WEIGHT = {info:1,minor:2,major:3,critical:4}`. For each finding,
skip unless `finding.is_odoo_specific`; build `haystack = (title + " " + body).lower()`; first
matching rule wins; `new = max(weight(current), weight(rule_floor))` (monotonic ⇒ never downgrades);
if changed, `finding.model_copy(update={"severity": new})` and log `odoo_severity_floored`.

**Rule table** (only deterministic, keyword-detectable, floor-safe rules):

| Floor | Anti-pattern (odoo19.md) | Match cue |
|---|---|---|
| critical | raw `cr.execute` w/ string formatting (SQLi) | `cr.execute`/`cursor.execute` **AND** an injection cue (f-string, `format(`, concat, "sql injection") |
| major | `cr.commit`/`cr.rollback` in business logic | `cr.commit`/`cr.rollback` |
| major | missing `ir.model.access.csv` for new model | `ir.model.access` **AND** (`missing`/`no access`/`without`) |
| major | `sudo()` in controller/public method | `sudo()` **AND** (`controller`/`public`/`auth=`/`without validation`) |
| major | controller `auth='none'` | `auth='none'`/`auth="none"`/`auth=none` |
| major | `@api.depends` missing a field | `api.depends` **AND** (`missing`/`incomplete`/`stale`) |
| major | `@api.onchange` writing to the DB | `api.onchange` **AND** (`write`/`create`/`persist`) |
| major | inline `<script>`/external CDN blocked by CSP | (`inline <script`/`external cdn`/`csp`) **AND** `script` |
| major | `__manifest__.py` missing a runtime dep | `__manifest__` **AND** `depends` **AND** (`missing`/`incomplete`) |

**Deliberately excluded** (judgement-dependent → over-floor risk): general `sudo()`, destructive
migration ops, N+1 ("at production volumes"), and all *minor* deprecation rules (`t-esc`, `name_get`,
`group_operator`, `distutils`, …) — flooring info→minor adds noise without changing the conclusion.

**Config/schema.** None. Severity persists through the existing `review_findings.severity` column;
calibration runs before persistence.

**Key edge cases.** Safe-but-mentioned `cr.execute` (no injection cue) is **not** floored;
non-`is_odoo_specific` findings are skipped entirely; model-raised-higher severity is preserved.

**Tests**

- `test_reviewer.py` — floor `cr.execute`-string-fmt minor→critical; `cr.commit` minor→major;
  missing-ACL minor→major; no-downgrade on a critical `cr.commit`; skip non-odoo; no false-floor on
  safe parameterized `cr.execute`; empty list; **integration** via `_make_reviewer` +
  `_claude_response_with_findings` proving a model-minor finding ends `critical` *and* lifts
  `result.risk_level` (i.e. calibration ran before `_recompute_risk_level`).
- `test_prompt_files.py` — `test_odoo_severity_rules_in_sync_with_odoo19_md`: each rule's anchor
  phrase is a substring of `odoo19.md`, so the table and the prompt can't silently drift.

> **Note (scope).** The repo-audit path produces findings through its own chain and is **not**
> calibrated — intentional, but an audit and a review could report the same anti-pattern at
> different severities. Flag for a future pass if that inconsistency matters.

---

## 3. Risk labels on the PR

**What/why.** `ReviewResult.risk_level` (low/medium/high/critical) is computed and persisted but
never surfaced on the PR list. After a completed review, set a single mutually-exclusive
`reva-risk-*` label, removing any prior one. Best-effort: a label failure never fails the review.

**Files touched**

- `reva/github_client.py` — add after `ensure_label` (ends ~line 385): `get_issue_labels` (GET
  `/repos/{o}/{r}/issues/{n}/labels` → `list[str]`, single page like `find_check_run_id`),
  `add_labels` (POST, additive — does **not** clobber human/CI labels), `remove_label` (DELETE,
  404-tolerant). Add a `_delete(token, path, allow_404=False)` helper mirroring `_patch` (~line 586).
- `worker/worker/runner.py` — add `RISK_LABEL_PREFIX` + `RISK_LABEL_META` constants and a
  best-effort `_set_risk_label(...)` helper (~after line 561); call it in `_post_result_to_github`'s
  **completed** branch only, after `_backfill_comment_ids` (~line 360). Whole helper body in
  `try/except Exception → log.warning("risk_label_failed"); return`.

**Reconciliation algorithm** (list + selective-remove + add — *not* PUT, which would clobber other labels)

```
target = RISK_LABEL_PREFIX + risk_level
ensure_label(target, color, desc)                  # idempotent; ensure_label already swallows 422
current = get_issue_labels(pr_number)
for name in current:
    if name.startswith(RISK_LABEL_PREFIX) and name != target:
        remove_label(pr_number, name)              # remove-before-add handles a downgrade
if target not in current:
    add_labels(pr_number, [target])
```

**Config/schema.** None. PRs and issues share `/issues/{n}/labels`, so `pr_number` is the issue number.

> ⚠️ **Verification BLOCKER.** `FakeGitHub` in `worker/tests/test_runner.py` (lines 52-103) has
> **none** of the new label methods. Until they're added, *every* completed-review test
> (`test_completed_run_posts_check_and_review`, `test_completed_with_no_findings_is_success`,
> `test_recovers_orphaned_pr_review_from_github`, …) will fail with `AttributeError` the moment
> `_set_risk_label` runs. **Extend `FakeGitHub` first** with `ensure_label`, `get_issue_labels`,
> `add_labels`, `remove_label` plus tracking fields (`existing_labels`, `added_labels`,
> `removed_labels`).

> ⚠️ **Verification fix — label naming.** Existing REVA labels use a **hyphen** (`reva-audit`,
> `reva-ticket`). The roadmap text said `reva:risk-*` (colon). Recommend **`reva-risk-*`** for
> consistency (see [Decisions](#decisions-needed)). Whichever is chosen, `remove_label` must
> URL-encode the name (`urllib.parse.quote(name, safe="")`) — a `:` in the DELETE path is otherwise malformed.

**Key edge cases.** Downgrade high→low (remove-before-add); same level (no-op); `remove_label`
404 swallowed; only completed reviews labelled (a decline's `risk_level` is a misleading placeholder);
label-API failure caught so it never bubbles a `TransientError` that would retry the whole job.

**Tests**

- `test_github_client.py` — `get_issue_labels` parses names; `add_labels` POSTs `{"labels":[...]}`;
  `remove_label` DELETEs the URL-encoded name; `remove_label` swallows 404.
- `test_runner.py` — completed review adds the right label + calls `ensure_label`; downgrade removes
  the prior `reva-risk-*` and leaves non-REVA labels untouched; same-level is a no-op; a raised
  exception in the label step still returns `status="completed"`; a declined review sets no label.

---

## 4. Trivial-diff short-circuit

**What/why.** `reviewer.py` runs a paid Claude call for any non-empty filtered diff. Detect diffs
that are *only* whitespace/comment/import-reorder changes and skip the call entirely — pure cost
savings, conservative by construction (anything ambiguous falls through to a real review).

**Files touched**

- `reva/diff_utils.py` — add pure `is_trivial_diff(diff) -> bool` + private helpers, after
  `parse_diff_hunks`/`find_line_in_hunks` (~line 202). Reuse the existing
  `re.split(r"(?=^diff --git )", ...)` section split (used at lines 108/138). Works on `+`/`-` body
  lines directly (`parse_diff_hunks` discards hunk bodies).
- `reva/types.py` — extend `ReviewStatus` (line 38) with `"skipped_trivial"`.
- `worker/worker/reviewer.py` — import `is_trivial_diff`; add a step **after** the skip_paths block
  (ends line 262) and **before** model selection (line 264): `if is_trivial_diff(diff): return
  _skipped_trivial()`; add module-level `_skipped_trivial()` next to `_decline` (~line 382).
- `worker/worker/runner.py` — add a `skipped_trivial` branch in `_post_result_to_github` mirroring
  the `stale` branch (post a `_post_simple_check_run(..., conclusion="skipped")`, no PR review/comment)
  and in `_execute_and_persist` call `writers.record_review_skipped_trivial(...)`.
- `reva/review_formatter.py` — `compute_check_conclusion` → `"skipped"` for `skipped_trivial`;
  add a `_check_run_title` + `format_check_run_output` arm.
- `reva/db/writers.py` — add `record_review_skipped_trivial` modeled on `record_review_stale`
  (~line 209): status row, zero findings, **no `_insert_spend`** (zero cost).

**Classifier logic.** Split into per-file sections. For each section, collect added (`+`, not `+++`)
and removed (`-`, not `---`) body lines. A section is trivial iff it is whitespace-only (lines equal
after stripping all whitespace), comment-only (`.py`: every non-blank added/removed line starts `#`),
or a pure Python import reorder (same import multiset, reordered). **Any** real added/removed line ⇒
the whole diff is non-trivial ⇒ return `False`. Deleted files (`+++ /dev/null`) ⇒ `False` (deletion
is substantive). New empty file as the sole change ⇒ trivial. Normalize trailing `\r` (CRLF) when
extracting body lines, matching `parse_diff_hunks` (`diff_utils.py:181`).

**Config/schema.** None — `review_runs.status` is plain TEXT with no CHECK constraint
(`001_initial.sql:66`). No per-repo toggle (none requested; Simplicity-First).

> ⚠️ **Verification fixes (two quiet bugs).**
> 1. `is_already_posted(db, params)` (`runner.py:~199` / `writers.py`) must treat
>    `skipped_trivial` as already-posted — otherwise an RQ retry of a trivial diff re-runs and
>    re-posts. (The plan's own note assumed "any non-`failed` is posted"; the code actually
>    enumerates `failed`/`declined`/`stale`/`completed`, so `skipped_trivial` must be **added**.)
> 2. `format_check_run_output`/`_footer` (`review_formatter.py:~156`) append cost/model metadata;
>    the `skipped_trivial` arm should **not** show cost/model (there was none). Give it its own
>    short "no substantive changes" body.

**Key edge cases.** Mixed diff (one trivial file + one real change) ⇒ non-trivial (the false-negative
safety case); add-a-line-and-reindent ⇒ non-trivial; import *added* (set differs) ⇒ non-trivial;
`.xml`/`.po`/`.pot`/`.md` are already stripped by `filter_diff` before this runs, so XML comment
handling is moot (don't build it). Comment detection is line-prefix based, not a tokenizer — at worst
it *skips* a pathological case, never falsely reviews; do **not** over-engineer a parser.

**Tests**

- `test_diff_utils.py` — whitespace-only, trailing-whitespace, Python comment-only, import reorder →
  `True`; import added, comment+real line, add+reindent, deleted file, new-file-with-content,
  mixed two-file, unknown-extension real change → `False`; new empty file, CRLF whitespace-only → `True`.
- `test_reviewer.py` — trivial diff ⇒ `status=="skipped_trivial"` and the runner/CLI is **never**
  called; skip happens *after* skip_paths filtering; mixed diff ⇒ `completed`; structured log event emitted.
- `test_runner.py` — `skipped_trivial` posts exactly one Check Run (`conclusion=="skipped"`), no PR
  review, no issue comment; persisted row has `status="skipped_trivial"`, `finding_count=0`.
- `test_review_formatter.py` — `compute_check_conclusion(...) == "skipped"`; non-error title + a
  "Skipped" body.

---

## Decisions needed

All have a recommended default; implementation can proceed on the defaults unless you say otherwise.

| # | Decision | Recommended default |
|---|---|---|
| A | **Risk-label naming** — `reva-risk-*` (hyphen, matches `reva-audit`/`reva-ticket`) vs `reva:risk-*` (colon, as the roadmap text said). | **`reva-risk-*`** (codebase convention; consistent label UX). |
| B | **Trivial-diff Check Run conclusion** — `skipped` vs `success`. | **`skipped`** (matches the stale path; non-blocking; plainly not "we reviewed and found nothing"). |
| C | **Trivial-diff status** — new `skipped_trivial` status vs overloading `completed`. | **New `skipped_trivial`** (clean analytics; no spend row; no full PR-review body posted). |
| D | **Severity-gating `none`** — map to `success` (silent) vs `neutral` (visible, non-blocking). | **`success`** (only non-blocking conclusion without a yellow check). |
| E | **Invalid `block_on_severity` value** — fail loud vs silent fallback to `major`. | **Silent fallback** (consistent with the existing YAML-error fallback). |
| F | **Odoo calibration scope** — also gate on `repo_config.odoo`? Also floor the *minor* deprecation rules? | **No** to both (`is_odoo_specific` already scopes; minor floors add noise without changing the gate). |
| G | **Risk-label opt-out** — add a `RepoConfig` flag? | **No** (default-on, no flag; matches Simplicity-First). |

## Suggested order

Independent, so any order works, but by ascending risk:

1. **Severity gating** (S) — single call site, default preserves behavior.
2. **Odoo calibration** (S) — isolated pure helper.
3. **Risk labels** (S) — do the `FakeGitHub` fixture work first (the blocker), then settle decision A.
4. **Trivial-diff** (M) — most invasive (new status across 5 files); land the two `is_already_posted`
   / `_footer` fixes with it.

Each ships as its own commit + green `make test-worker` + `ruff check`.
