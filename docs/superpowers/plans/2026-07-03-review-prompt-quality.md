# Review-Prompt Quality Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise review quality across all six CLI skills: calibrated worked examples, a mandatory pre-output self-verification step, honest confidence scoring with code-side enforcement, a verbatim `code_excerpt` evidence anchor threaded through ground-check → verifier → DB, an `odoo_version` repo config so non-19 repos stop getting wrong-version deprecation noise, deduplicated shared prompt blocks, a defined summary shape, severities for every `odoo19.md` rule, and removal of the dead Messages-API review prompt path.

**Architecture:** All prompt edits land under one version bump (**v2.1**) so the drift guard re-baselines once. Code changes ride existing seams: the confidence filter and excerpt ground-check join the existing step-12 finding pipeline in `Reviewer.execute` (`worker/worker/reviewer.py:568-595`); `code_excerpt` extends `Finding` (`reva/types.py:84`), the `review_findings` table (migration `024`), and `StoredFinding`/`_finding_header` (`reva/finding_verifier.py:16,117`); `odoo_version` is a new optional `RepoConfig` field injected as a present-only nonce-fenced skill param (prompt-prefix stability preserved). No TUI change: `code_excerpt` is per-finding evidence detail, not at-a-glance data (CLAUDE.md §5 considered — the Feedback/Audits tabs already surface outcomes; nothing new is aggregate-worthy).

**Tech Stack:** Python 3.14, pytest (SQLite in-memory + mocks — no Docker/network), ruff. One SQL migration (Postgres-only constructs: none — plain `ADD COLUMN`).

**Spec:** No separate spec; the design rationale is this plan's task intros. Read `prompts/review_guidance.md`, `prompts/skills/*.md`, and `worker/worker/reviewer.py:440-627` first.

## Global Constraints

- Test venvs per CLAUDE.md (`worker/.venv` etc.; create if missing). Shared `reva/` is touched → definition of done is **all three** service suites (`make test`) plus `ruff check reva worker/worker api/app scheduler/scheduler`.
- **Prompt version:** every prompt edit in this plan belongs to **v2.1**. Task 1 opens the CHANGELOG heading and fixes `test_get_version_returns_current_version` (`worker/tests/test_prompt_files.py:84`); later tasks only append bullets. Never edit a prompt file without the v2.1 heading already present.
- SECU-6: repo-/developer-authored content enters prompts only inside per-call nonce fences. The CLI runner fences every skill param (`reva/claude_code_runner.py:280-292`) — never bypass it.
- Prompt-prefix stability: optional skill params are attached **only when present**. A repo without `odoo_version` must produce the exact same `skill_params` dict as today.
- Severity definitions, `block_on_severity` gating, and the `_ODOO_SEVERITY_RULES` floors keep their current semantics (Task 9 adds prompt-side severities only; no new floor rules).
- Fail-open discipline: like `_ground_findings` (`reviewer.py:939`), every new deterministic check drops a finding only on **positive evidence** of wrongness, never on an infrastructure failure (unreadable file, missing clone).
- Migration conventions (CLAUDE.md): numbered file `024_*.sql`, idempotent (`ADD COLUMN IF NOT EXISTS`), matching ORM column in `reva/db/models.py` (tests build from models).
- Commit style: conventional commits, each ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Open prompt version v2.1

**Files:**
- Modify: `prompts/CHANGELOG.md`
- Modify: `worker/tests/test_prompt_files.py` (line 84-85)

**Why first:** `PromptBuilder.get_version()` reads the first `##` heading; the drift guard re-baselines content hashes under a new version. All later prompt edits must land under v2.1 or the guard alerts on boot.

- [ ] **Step 1:** Update the assertion in `worker/tests/test_prompt_files.py:85` to `assert builder.get_version() == "v2.1"`. Run `cd worker && .venv/bin/python -m pytest tests/test_prompt_files.py -k version -v` — expect FAIL.
- [ ] **Step 2:** Prepend to `prompts/CHANGELOG.md`:

```markdown
## v2.1 — Review-quality pass: examples, self-verification, evidence anchors

- (bullets appended by the tasks in docs/superpowers/plans/2026-07-03-review-prompt-quality.md
  as they land; summary at plan completion.)
```

- [ ] **Step 3:** Re-run the test — PASS. Commit:

```bash
git add prompts/CHANGELOG.md worker/tests/test_prompt_files.py
git commit -m "docs(prompts): open prompt version v2.1

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Delete the dead Messages-API review prompt path

**Files:**
- Delete: `prompts/system.md`, `prompts/diff_review.md`, `prompts/deep_review.md`
- Modify: `reva/prompt_builder.py` (remove `build_system_blocks` :31-64 and `build_user_prompt` :68-86; rewrite the module docstring's cache-strategy block :1-13)
- Modify: `reva/types.py:205` (ContentBlock comment references system.md)
- Modify: `prompts/README.md` (file inventory)
- Test: `worker/tests/test_prompt_files.py` (remove tests exercising the deleted methods)

**Why:** Grep-verified: no production caller. `ticket_analyzer._build_system` reads `ticket_analysis.md` directly; `reply_runner.py` builds its system prompt inline; the CLI review path reads `review_guidance.md`/`odoo19.md`/skills. `system.md` still carries a `submit_review` contract, a 15-finding cap, and self-set `risk_level` instructions that contradict `review_guidance.md:129` — dead-but-contradictory text someone will eventually "fix" a live review by editing. `compute_prompt_hashes` never hashed these files, so no drift-guard interaction.

**Kept:** `reva/review_tool.py` (`build_review_tool_schema` derives the CLI output shape docs and is exercised by tests) — verify with grep before assuming; if it too has no production caller, note that in the commit message but do **not** delete it in this plan (out of scope).

- [ ] **Step 1:** `grep -rn 'build_system_blocks\|build_user_prompt\|system\.md\|diff_review\.md\|deep_review\.md' --include='*.py' reva worker api scheduler` — confirm only `prompt_builder.py`, `types.py:205`, and tests hit.
- [ ] **Step 2:** Delete the three files and the two methods; update the docstring (PromptBuilder now = versioning + hashes only), `types.py` comment, `prompts/README.md`.
- [ ] **Step 3:** Remove/adjust the affected tests in `worker/tests/test_prompt_files.py` (keep every `get_version`/`compute_prompt_hashes` test).
- [ ] **Step 4:** Full suites + ruff (`make test`; reva touched). Append a CHANGELOG bullet under v2.1 ("removed dead Messages-API review prompts: system.md, diff_review.md, deep_review.md"). Commit `refactor(prompts): remove dead Messages-API review prompt path`.

---

### Task 3: Deduplicate the "Team configuration" block into `review_guidance.md`

**Files:**
- Modify: `prompts/review_guidance.md` (add section)
- Modify: `prompts/skills/reva-diff-review.md`, `reva-delta-review.md`, `reva-full-review.md`, `reva-migration-review.md`, `reva-xml-review.md` (remove the verbatim block)
- Modify: `prompts/skills/README.md` (note the convention: shared behavior lives in the guidance; skills carry only mode-specific deltas)
- Test: `worker/tests/test_prompt_files.py`

**Why:** The three-paragraph "Team configuration (when present)" block (`custom_instructions` / `muted_categories` / `team_review_preferences`) is copied verbatim in 5 skills — drift waiting to happen. `review_guidance.md` is prepended to **every** skill (`_build_preamble`, `reva/claude_code_runner.py:495`), including `reva-repo-audit` — safe because the block is written as "when present" and audits simply never receive those params.

**Scope guard:** move ONLY this block. The "Security-model consistency" (3 variants) and "Manifest checks" (4 variants) sections differ deliberately by mode-scoping — leave them in place.

- [ ] **Step 1:** Add a test asserting `review_guidance.md` contains the `team_review_preferences` paragraph and that no `skills/*.md` file contains the phrase "If a `custom_instructions` parameter is present" (loop over the skills dir).  Run — FAIL.
- [ ] **Step 2:** Move the block into `review_guidance.md` as `## Team configuration parameters (when present)` (verbatim content from the skills, singular source). Delete the section from the five skills.
- [ ] **Step 3:** Tests PASS; `make test` + ruff. CHANGELOG bullet. Commit `refactor(prompts): single-source the team-configuration block in review_guidance`.

---

### Task 4: Code-side minimum-confidence enforcement

**Files:**
- Modify: `worker/worker/reviewer.py` (new module constant + filter in step 12)
- Test: `worker/tests/test_reviewer.py`

**Why:** Today the ≥ 0.7 bar exists only as a prompt instruction — which trains the model to *never emit* a number below 0.7 (borderline findings get inflated to exactly 0.7 rather than dropped), so confidence stops carrying signal for `_cap_findings` and the verifier. Task 5 changes the prompt to "score honestly; the system filters" — this task adds the filter that makes that true, plus telemetry on what gets dropped.

- [ ] **Step 1: Failing tests** in `worker/tests/test_reviewer.py`:
  - a finding with `confidence=0.69` is absent from `ReviewResult.findings`; one with `0.7` survives;
  - dropped findings never reach `_verify_findings` (assert the verifier fake is not called for them);
  - the drop is logged (`findings_dropped_low_confidence` with `count`).
- [ ] **Step 2:** In `worker/worker/reviewer.py`, next to `MAX_FINDINGS` (:56), add `MIN_CONFIDENCE = 0.7`. In `execute` step 12, after `_drop_muted_findings` (:576) and **before** `_calibrate_odoo_severity` / the verify pass (don't pay to verify sub-threshold findings):

```python
        # Enforce the confidence floor in code, not just in the prompt: the
        # prompt now asks for honest scores (v2.1), so sub-0.7 findings are
        # expected output, dropped here with telemetry instead of being
        # inflation-laundered to 0.7 by the model.
        confident = [f for f in grounded if f.confidence >= MIN_CONFIDENCE]
        if len(confident) < len(grounded):
            logger.info(
                "findings_dropped_low_confidence",
                count=len(grounded) - len(confident),
                titles=[f.title for f in grounded if f.confidence < MIN_CONFIDENCE],
            )
        grounded = confident
```

- [ ] **Step 3:** Tests PASS; worker suite + ruff. Commit `feat(review): enforce the 0.7 confidence floor in code with drop telemetry`.

---

### Task 5: `code_excerpt` evidence anchor — schema, ground-check, verifier, persistence

**Files:**
- Modify: `reva/types.py` (Finding, :84-128)
- Create: `db/migrations/024_finding_code_excerpt.sql`
- Modify: `reva/db/models.py` (ReviewFinding, :206)
- Modify: `reva/db/writers.py` (finding insert, :1841; the reader(s) that build `StoredFinding` rows for delta-resolution — locate via `grep -n 'StoredFinding(' reva worker`)
- Modify: `reva/finding_verifier.py` (`StoredFinding` :16, `_finding_header` :117)
- Modify: `worker/worker/reviewer.py` (`_verify_findings` StoredFinding construction; new `_drop_unmatched_excerpts` in step 12)
- Modify: all 6 `prompts/skills/*.md` (output JSON + one instruction line)
- Tests: `worker/tests/test_reviewer.py`, `worker/tests/test_finding_verifier.py`, DB writer tests, `worker/tests/test_prompt_files.py`

**Why:** Findings carry only `line_start` + prose. Requiring a verbatim quote of the offending line (a) forces the model to re-read the cited line before emitting — killing hallucinated line numbers; (b) gives both verifier passes (`_finding_header` currently renders title/severity/location/body only) a concrete string to locate — this also improves **delta-resolution** verdicts on later pushes, which is why it must be persisted; (c) a quote that does not appear in the file is a free deterministic false-positive signal checked *before* any paid verifier call.

**Matching rules (the load-bearing design):** normalize by taking the excerpt's **first non-empty line**, stripping one leading `+`/`-`, collapsing whitespace runs to single spaces, and trimming. Skip the check entirely (keep the finding, keep the excerpt) when: the finding has no excerpt, no `file`, the normalized line is shorter than 10 chars, or the file is unreadable (fail-open). Drop the finding only when the file was read and the normalized line is not a substring of any normalized file line. Log `findings_dropped_excerpt_unmatched` with titles.

- [ ] **Step 1: Failing tests.**
  - `types`: `Finding(code_excerpt=...)` accepted, defaults to `None`, truncated at 1000 chars (mirror the body-truncation validator pattern, :107).
  - `reviewer`: excerpt matching table — exact match kept; match with `+` prefix and extra indentation kept; unmatched ≥10-char excerpt dropped; <10-char excerpt kept unchecked; missing file → kept (ground-check drops it separately); unreadable file → kept.
  - `verifier`: `_finding_header` output contains a `**Cited code:**` line iff `StoredFinding.code_excerpt` is set.
  - `writers`: insert + read-back round-trips `code_excerpt` (SQLite via ORM).
- [ ] **Step 2:** `reva/types.py`: add `code_excerpt: str | None = None` with a 1000-char truncation validator.
- [ ] **Step 3:** Migration `024_finding_code_excerpt.sql`:

```sql
ALTER TABLE review_findings ADD COLUMN IF NOT EXISTS code_excerpt TEXT;
```

  ORM: `code_excerpt: Mapped[str | None]` on `ReviewFinding`. Writer insert (:1841) gains `code_excerpt=f.code_excerpt`; every query that constructs `StoredFinding` for delta-resolution/verification selects and passes it.
- [ ] **Step 4:** `reva/finding_verifier.py`: `StoredFinding` gains `code_excerpt: str | None = None`; `_finding_header` appends `f"\n**Cited code:** `{finding.code_excerpt}`"` when set. (Header is REVA-rendered from our own DB row — same trust level as title/body, which already ride unfenced; no new fencing needed, note this in a comment.)
- [ ] **Step 5:** `worker/worker/reviewer.py`: implement `_drop_unmatched_excerpts(findings, repo_path)` per the matching rules; call it in step 12 immediately after `_ground_findings` (:570). Update the in-process `StoredFinding` construction in `_verify_findings` to pass the excerpt.
- [ ] **Step 6:** Skills: add to each output-format JSON example `"code_excerpt": "cr.execute(f\"... {barcode} ...\")"` and this bullet under the format notes:

```markdown
- `code_excerpt`: the exact offending line(s), copied verbatim from the
  post-change file via Read (not re-typed, not from the diff's `+` prefix).
  1-3 lines. REVA drops findings whose excerpt does not appear in the file,
  so copy exactly. `null` only for findings with no specific location.
```

- [ ] **Step 7:** All suites (`make test` — reva + worker touched) + ruff. Note honestly: the migration's raw SQL is exercised only on real Postgres → run `make test-integration` or flag for first staging boot (CLAUDE.md). CHANGELOG bullet. Commit `feat(review): verbatim code_excerpt evidence anchor through ground-check, verifier, and DB`.

---

### Task 6: "Verify before you write" + honest confidence (prompt side)

**Files:**
- Modify: `prompts/review_guidance.md` (rewrite "Confidence scoring" :69-79; new section after "Conduct rules")
- Modify: all 6 skills (process-list line "Keep only findings you are confident about (confidence ≥ 0.7)")
- Test: `worker/tests/test_prompt_files.py`

**Why:** The skills' review process ends at "keep only ≥ 0.7" with no instruction to verify anything, and rule 1 asks for diff-mappable line numbers without saying how to get them (models get hunk arithmetic wrong; they have Read). This targets the classic FP modes: misread citation, "X is unused" without grepping, flagging behavior the parent class already handles.

- [ ] **Step 1:** Failing content tests: guidance contains "Verify before you write"; no skill contains the string "confidence ≥ 0.7" (the bar is now stated once, honestly, in the guidance).
- [ ] **Step 2:** Add to `review_guidance.md`:

```markdown
## Verify before you write

Before emitting each finding, spend the tool calls to verify it:

1. **Re-Read the exact lines you cite.** Take `line_start`/`line_end` from
   Read of the post-change file — never by counting diff hunk lines. Copy
   `code_excerpt` verbatim from that Read.
2. **Claims of absence need a search.** Before "X is unused / never called /
   missing", Grep for it (callers, ACL row, test file). One grep beats one
   retraction.
3. **Check the framework didn't already handle it.** For "missing check /
   missing handling" findings on an override, Read the parent method or the
   framework hook first.
4. A finding that fails its own verification is not reported — it is either
   fixed (you found the real location) or dropped.
```

  Rewrite the confidence section: delete "Report only findings with confidence ≥ 0.7"; replace with:

```markdown
Score honestly — the system enforces the reporting threshold, not you. If a
finding lands below 0.7, do not inflate the number: either gather more
evidence with Read/Grep until you genuinely believe it, or let it go. An
honest 0.6 that gets filtered is better than a false 0.7 that wastes a
developer's time.
```

- [ ] **Step 3:** In each skill's process list, replace the "Keep only findings…≥ 0.7" step with: `Verify each candidate finding per the guidance ("Verify before you write"), then score confidence honestly.`
- [ ] **Step 4:** Tests PASS; worker suite + ruff. CHANGELOG bullet. Commit `feat(prompts): mandatory pre-output verification pass + honest confidence scoring`.

---

### Task 7: Worked examples (calibration anchors) in `review_guidance.md`

**Files:**
- Modify: `prompts/review_guidance.md` (new section after "Verify before you write")
- Test: `worker/tests/test_prompt_files.py`

**Why:** The prompts define severity/confidence abstractly but never show a model of excellence or a withheld borderline case. Examples calibrate; definitions don't. Placed in the guidance so all six skills inherit them. Uses `code_excerpt` — Task 5 must land first.

- [ ] **Step 1:** Failing content test: guidance contains "Worked examples".
- [ ] **Step 2:** Add (exact content):

````markdown
## Worked examples

**Report this — evidence-first, reachable, concrete fix:**

```json
{
  "severity": "critical",
  "category": "security",
  "file": "custom_addons/stock_sync/controllers/main.py",
  "line_start": 49,
  "line_end": 49,
  "title": "SQL injection via f-string in cr.execute on user-supplied barcode",
  "body": "`_lookup_moves` interpolates the request parameter `barcode` straight into SQL. A crafted barcode escapes the quote and runs arbitrary SQL as the DB user. The route above is `auth='public'` (line 42), so this is reachable without login.",
  "suggestion": "Parameterize: cr.execute(\"SELECT id FROM stock_move WHERE ref = %s\", (barcode,)) — or use the ORM: env['stock.move'].search([('ref', '=', barcode)]).",
  "confidence": 0.95,
  "is_odoo_specific": true,
  "code_excerpt": "self.env.cr.execute(f\"SELECT id FROM stock_move WHERE ref = '{barcode}'\")"
}
```

What makes it good: the offending line is quoted verbatim, reachability is
stated (public route), the fix is minimal and idiomatic, and the severity
follows the definitions (unauthenticated SQL injection = critical).

**Do NOT report this — speculative, no evidence gathered:**

> "The new `_compute_totals` method might be slow on large recordsets."

No loop over records reading relational fields was identified, no data-volume
argument made. Honest confidence ≈ 0.4. Either Read the method and find the
actual N+1 access (then report it with the line quoted), or drop it.

**Consolidate repeats — one finding, occurrences listed:**

> title: "Untranslated user-facing error strings in raise UserError"
> body: "…same pattern at models/sale_order.py:88, models/picking.py:41."

One finding with the other locations named — never one finding per occurrence.
````

- [ ] **Step 3:** Tests PASS; worker suite + ruff. CHANGELOG bullet. Commit `feat(prompts): worked calibration examples in review guidance`.

---

### Task 8: Summary contract

**Files:**
- Modify: `prompts/review_guidance.md` (new short section)
- Modify: all 6 skills (the `"summary"` description string in the output JSON example)
- Test: `worker/tests/test_prompt_files.py`

**Why:** Skills say only "2-3 sentence overview". On a clean PR the summary *is* the whole review, and a bare "looks good" builds no trust and proves no work. Asking for "what was verified" also forces the verification to happen.

- [ ] **Step 1:** Failing content test: guidance contains "Summary contract".
- [ ] **Step 2:** Add to `review_guidance.md`:

```markdown
## Summary contract

The `summary` is 2-4 sentences with this shape:

1. What the change does (one sentence, from the diff — not the PR title's claim).
2. The top concern, or an explicit "no findings" statement.
3. What you verified clean — the checks you ran that found nothing (e.g.
   "checked callers of action_confirm, the new model's ACL, and the compute
   dependencies; all consistent"). This line is mandatory on clean reviews.
```

  In each skill's output JSON, change the summary placeholder to `"summary": "What the PR does; top concern or none; what was verified clean (see guidance: Summary contract)"` (keep the delta skill's "new changes" wording flavor).
- [ ] **Step 3:** Tests PASS; worker suite + ruff. CHANGELOG bullet. Commit `feat(prompts): defined summary shape incl. what-was-verified line`.

---

### Task 9: Severity labels for every `odoo19.md` rule (+ fix the JSONB version inconsistency)

**Files:**
- Modify: `prompts/odoo19.md`
- Test: none (prompt content; the existing `_ODOO_SEVERITY_RULES` floors are untouched)

**Why:** Most rules carry explicit severities; the unlabeled ones drift run-to-run in a way the Tier-0 deterministic calibration can't floor (it only floors rules it knows). Also `odoo19.md:27` says translations JSONB is "16+" while `:64` says "17+" — JSONB translations landed in Odoo 16; make both say 16+.

- [ ] **Step 1:** Assign severities to the unlabeled rules (edit each line in place):
  - `with_context()` misuse (:16) → **Minor**
  - `search_count()` limit awareness (:24) → **Minor**
  - `_search_display_name` preference (:25) → **Info**
  - explicit `inherit_id` required (:40) → **Major** (runtime inheritance failure)
  - `<card>` in Kanban (:41) → **Minor** (matches the XML skill)
  - `_read_group()` signature misuse (:47) → **Major**
  - `mapped()` on large recordsets (:48) → **Minor**
  - file-naming conventions (:55) → **Minor**
  - JSONB translations format (:64) → **Major** (matches the migration skill) + unify "16+"
  - Python 3.12 type-system patterns (:71) → **Info**
- [ ] **Step 2:** worker suite (prompt-content tests) + ruff untouched. CHANGELOG bullet. Commit `fix(prompts): explicit severity on every odoo19.md rule`.

---

### Task 10: `odoo_version` repo config — stop flagging Odoo-19 deprecations on older repos

**Files:**
- Modify: `reva/types.py` (RepoConfig, :48-78)
- Modify: `worker/worker/reviewer.py` (optional skill param, near the `muted_categories` injection :480-484)
- Modify: `prompts/odoo19.md` (version annotations + header note)
- Modify: root `README.md` (`.claude-review.yml` key list) and the CLAUDE.md per-repo-overrides bullet
- Tests: `worker/tests/test_reviewer.py` (config parse + param injection)

**Why:** `RepoConfig.odoo` is a bare bool (:67), so a repo on 16/17/18 gets `odoo19.md`'s 19-only deprecation/rename rules (`_sql_constraints` → `Constraint`, `group_operator` → `aggregator`, `inselect`, `_flush_search`, …) as review criteria — wrong-version noise, worst exactly during an upgrade window. Minimal fix: annotate rule applicability in the one rules file and tell the model the repo's target version via an optional param. No new per-version rule files (speculative until a real non-19 repo needs more).

- [ ] **Step 1: Failing tests.**
  - `RepoConfig` parses `odoo_version: 17` (int, `ge=12`); absent → `None`.
  - With `odoo: true, odoo_version: 17` → `skill_params["odoo_version"]` present with the note text; with `odoo_version: 19` or unset → key absent (prompt-prefix stability).
- [ ] **Step 2:** `reva/types.py`: `odoo_version: int | None = Field(default=None, ge=12)` with a comment ("target Odoo major; None = assume current (19). Only consulted when `odoo: true`.").
- [ ] **Step 3:** `worker/worker/reviewer.py`, after the muted-categories injection:

```python
        # Non-19 Odoo repos: the rules preamble is written for 19; tell the
        # model the real target so 19-only deprecation/rename rules aren't
        # flagged as issues. Optional param (prompt-prefix stability).
        if repo_config.odoo and repo_config.odoo_version and repo_config.odoo_version != 19:
            skill_params["odoo_version"] = (
                f"This repository targets Odoo {repo_config.odoo_version}. The Odoo "
                f"rules in the guidance assume Odoo 19 — skip any rule marked for a "
                f"version newer than {repo_config.odoo_version}, and never flag the "
                f"absence of a newer-version API as an issue."
            )
```

- [ ] **Step 4:** `prompts/odoo19.md`: add under the title: *"Rules marked `(19)` / `(17+)` / `(16+)` apply only when the repo's target version qualifies; an `odoo_version` task parameter, when present, states the target."* Annotate: `(19)` — `_sql_constraints`/Constraint, `inselect`, `group_operator`→`aggregator`, `_flush_search`, `search_count` limit, `_search_display_name`, record-rule OR logic, `check_access`/`has_access`, `search_fetch` note if 17+ (verify: `search_fetch` landed in 17 → `(17+)`), `GROUPING SETS`/`any!`, `<card>` Kanban, explicit `inherit_id`, Python-3.11 minimum; `(17+)` — `name_get`→`display_name`; `(16+)` — `t-esc`→`t-out`, JSONB translations. Version-agnostic rules (SQL injection, `cr.commit`, `@api.depends`, `sudo()`, ACL/ir.rule, N+1, manifest checks) stay unmarked.
- [ ] **Step 5:** Docs: add `odoo_version` to the README `.claude-review.yml` example and the CLAUDE.md overrides list. All suites + ruff. CHANGELOG bullet. Commit `feat(review): odoo_version repo config gates version-specific rules`.

---

### Task 11: Finalize — CHANGELOG, full definition of done, staging notes

- [ ] **Step 1:** Rewrite the v2.1 CHANGELOG entry as a coherent summary of everything above (examples, verify-before-write, honest confidence + code floor, `code_excerpt`, summary contract, team-config dedup, odoo19 severities + version annotations, dead-file removal, `odoo_version`).
- [ ] **Step 2:** Definition of done (CLAUDE.md): `make test` (worker + api + scheduler — shared `reva/` changed), `ruff check reva worker/worker api/app scheduler/scheduler`, and `make test-integration` for migration `024` (or state explicitly it awaits first staging boot). `tui/` untouched — no Go gate.
- [ ] **Step 3:** Update `HANDOFF.md`: add this plan to the resume section; note the two follow-ups this plan deliberately does **not** cover:
  - **Measurement gap:** none of these changes is measurable without the golden-PR replay harness (Tier 3, `FEATURE_ROADMAP.md`) — recommend it as the next tier-3 item, seeded with a handful of PRs reviewed before/after v2.1.
  - **Live-CLI staging gate** (A1/A2 pattern): run one real diff review and one delta review on staging; watch for `code_excerpt` population rate, `findings_dropped_excerpt_unmatched` false-drop rate (a high rate means the matching normalization needs loosening — the excerpt check is the one risky drop in this plan), `findings_dropped_low_confidence` volume, and summary shape adherence.
- [ ] **Step 4:** Commit `docs: v2.1 changelog + handoff for review-quality pass`.

---

## Task dependency notes

- Task 1 gates every prompt-touching task (2, 3, 5-10).
- Task 5 (`code_excerpt`) gates Task 7 (examples reference the field).
- Task 4 (code floor) and Task 6 (honest-confidence prompt) ship in either order but **both must land before deploy** — the prompt change without the code filter would post sub-0.7 findings.
- Tasks 2, 3, 8, 9, 10 are independent of each other.

## Explicitly out of scope (decided during planning)

- Consolidating the mode-scoped "Security-model consistency" / "Manifest checks" skill variants — their differences are intentional.
- New per-version Odoo rule files (`odoo16.md`, …) — speculative until a non-19 repo needs more than the annotation approach.
- Routing 0.5-0.7-confidence findings to the Haiku verifier instead of dropping them — revisit once `findings_dropped_low_confidence` telemetry shows what's being lost.
- TUI changes — no new aggregate data; `code_excerpt` is per-finding detail (CLAUDE.md §5 considered).
- Deleting `reva/review_tool.py` even if dead — separate cleanup decision.
