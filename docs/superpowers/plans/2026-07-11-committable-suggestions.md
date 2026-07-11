# Committable Suggestion Patches — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Findings with a line anchor and a full-replacement `suggestion` render as GitHub-native ` ```suggestion ` blocks on the inline PR comment — one click ("Commit suggestion") applies the fix — gated by a per-repo `commit_suggestions` toggle (default on) with a deterministic downgrade-to-plain-text guard.

**Spec:** `docs/superpowers/specs/2026-07-11-committable-suggestions-design.md` (approved 2026-07-11).

**Architecture:** The formatter (`reva/review_formatter.py`) is pure and receives no repo config today; the established carry pattern is a **transient field on `ReviewResult`** (like `diff` and `block_on_severity`, see `reva/types.py:203-210`) set in `Reviewer.execute` from `RepoConfig` and read by `runner._post_completed_review`. This plan adds `RepoConfig.commit_suggestions` → `ReviewResult.commit_suggestions` → a `commit_suggestions` parameter on `format_inline_comment(_payload)`. GitHub semantics the guard encodes: a ` ```suggestion ` fence inside an inline review comment replaces **exactly** the commented line range (single-line `line`, or multi-line `start_line`..`line` — the exact range `format_inline_comment_payload` already anchors); the fence content must be the complete replacement lines (whitespace-sensitive — a trailing newline inside the fence commits an extra blank line); an un-anchored comment cannot carry a suggestion, so a missing anchor downgrades to plain text. The five inline-review skill prompts get a tightened `suggestion` output contract (exact replacement, no prose/ellipses/fences, else `null`). **Spec wording note discovered during planning:** the spec says "Prose advice belongs in `description`" — the actual `Finding` field is `body` (`reva/types.py:114`); the prompts and this plan use `body`, and Task 4 corrects the spec text.

**Tech Stack:** Python 3.14, pydantic 2 (`reva/types.py` models), pytest per-service venvs, markdown prompt files under `prompts/skills/`. No new dependencies, no DB changes, no TUI changes.

## Global Constraints

- **RepoConfig wave coordination:** `reva/types.py` `RepoConfig`: this wave adds `commit_suggestions: bool = True` (THIS plan), plus `describe_command`, `fix_command`, `issue_plans` from sibling plans. This plan adds ONLY `commit_suggestions` + its README config-table row.
- **prompts/CHANGELOG.md:** top entry is currently v2.8 (unshipped). Wave rule: ALL five features share ONE **v2.9** entry — create the v2.9 entry if absent, otherwise append a bullet to the existing v2.9. Never mint v2.10. (`worker/tests/test_prompt_files.py:47` asserts the parsed version — update it to `"v2.9"` only if a sibling plan hasn't already.)
- **No changes to `reva/odoo_contracts.py` or `contracts/`** (this wave has no Odoo contract impact).
- **No DB migrations.**
- **Ops-event invariant (CLAUDE.md):** any caught-and-degraded path must log AND `writers.record_ops_event(...)`. This feature's downgrade-to-text guard is a **deterministic rendering rule, NOT a degradation — no ops event for it**; the suggestion is never dropped, only rendered as plain text.
- **Injection posture unchanged (spec §5):** suggestion text is model output already posted to GitHub today; existing internal-path redaction applies. No new sanitization code.
- **Out of scope (spec):** multi-line-range/multi-file suggestion batches, auto-committing (`/fix` is a separate spec), retro-fitting old findings. `reva-repo-audit.md` keeps its current `suggestion` wording — audit findings become GitHub issues, not inline comments.
- **Final verification:** `make test` green (shared `reva/` touched → worker, api, scheduler), `worker/.venv/bin/ruff check reva worker/worker api/app scheduler/scheduler` clean. No `tui/` changes expected in this plan (nothing new to surface at a glance — the rendering change lives on GitHub).
- Per-service venvs: `cd worker && .venv/bin/python -m pytest tests/...`.

---

### Task 0: Preconditions

**Files:** none (checks only).

- [ ] **Step 1: Verify a clean tree at/after the spec commit**

Run from `/home/joseph/Projects/Cloudunify/cu_reva`:

```bash
git status --porcelain
git merge-base --is-ancestor e10364b HEAD && echo ANCESTOR_OK
test -f docs/superpowers/specs/2026-07-11-committable-suggestions-design.md && echo SPEC_OK
```

Expected: empty output from `git status --porcelain` (a sibling wave plan may already be merged — that is fine, only *uncommitted* changes block), then `ANCESTOR_OK`, then `SPEC_OK`. If any check fails, STOP and report — do not proceed on a dirty tree or a stale base.

---

### Task 1: Formatter — committable ` ```suggestion ` block + deterministic downgrade guard

**Files:**
- Modify: `reva/review_formatter.py:305-351` (`format_inline_comment` at 308, `format_inline_comment_payload` at 331)
- Test: `worker/tests/test_review_formatter.py` (inline-comment section, after line 226; helper `_f` at line 20 already accepts `suggestion=` and sets `line_end=line_start`)

**Interfaces:**
- Consumes: `Finding` (`reva/types.py:105` — fields `suggestion: str | None`, `line_start: int | None`, `line_end: int | None`, `file: str | None`).
- Produces (Task 2 calls these):
  - `format_inline_comment(finding: Finding, commit_suggestions: bool = True) -> str`
  - `format_inline_comment_payload(finding: Finding, commit_suggestions: bool = True) -> dict`
  - module constant `MAX_SUGGESTION_LINES = 30`
  - module-private `_is_committable_suggestion(finding: Finding, commit_suggestions: bool) -> bool`

- [ ] **Step 1: Write the failing tests**

Append to the `# --- inline comment ---` section of `worker/tests/test_review_formatter.py` (after `test_inline_comment_includes_suggestion_block_when_provided`, line 226):

```python
def test_inline_comment_renders_committable_suggestion_block():
    text = format_inline_comment(
        _f("minor", file="x.py", line_start=5, suggestion="    return foo(bar)")
    )
    assert "**Suggestion**" in text
    assert "```suggestion\n    return foo(bar)\n```" in text


def test_suggestion_block_content_is_byte_exact():
    # GitHub is whitespace-sensitive inside the fence: indentation and internal
    # blank lines are replacement content; ONLY trailing newlines are stripped
    # (they would otherwise commit an extra empty line).
    sugg = "if x:\n\n    do()\n"
    text = format_inline_comment(_f("minor", file="x.py", line_start=5, suggestion=sugg))
    assert "```suggestion\nif x:\n\n    do()\n```" in text


def test_suggestion_toggle_off_renders_plain_block():
    text = format_inline_comment(
        _f("minor", file="x.py", line_start=5, suggestion="return foo(bar)"),
        commit_suggestions=False,
    )
    assert "```suggestion" not in text
    assert "```\nreturn foo(bar)\n```" in text  # current plain rendering, unchanged


def test_suggestion_without_anchor_renders_plain_block():
    # No line_start = no line range for GitHub to apply the suggestion to.
    text = format_inline_comment(
        _f("minor", file="x.py", line_start=None, suggestion="return foo(bar)")
    )
    assert "```suggestion" not in text
    assert "return foo(bar)" in text


def test_suggestion_containing_fence_renders_plain_block():
    # A nested fence would terminate the ```suggestion block early.
    text = format_inline_comment(
        _f("minor", file="x.py", line_start=5, suggestion="```python\nfoo()\n```")
    )
    assert "```suggestion" not in text


def test_suggestion_over_30_lines_renders_plain_block():
    sugg = "\n".join(f"line{i}" for i in range(31))  # 31 lines
    text = format_inline_comment(_f("minor", file="x.py", line_start=5, suggestion=sugg))
    assert "```suggestion" not in text
    assert "line30" in text  # never dropped — degraded to text


def test_suggestion_exactly_30_lines_is_committable():
    sugg = "\n".join(f"line{i}" for i in range(30))  # boundary: 30 lines OK
    text = format_inline_comment(_f("minor", file="x.py", line_start=5, suggestion=sugg))
    assert "```suggestion" in text


def test_whitespace_only_suggestion_renders_plain_block():
    text = format_inline_comment(_f("minor", file="x.py", line_start=5, suggestion="   \n  "))
    assert "```suggestion" not in text


def test_inline_payload_carries_committable_block_and_honors_toggle():
    f = _f("major", file="a.py", line_start=3, suggestion="fixed = True")
    on = format_inline_comment_payload(f)
    off = format_inline_comment_payload(f, commit_suggestions=False)
    assert "```suggestion\nfixed = True\n```" in on["body"]
    assert "```suggestion" not in off["body"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_review_formatter.py -k suggestion -v`
Expected: `test_inline_comment_renders_committable_suggestion_block`, `test_suggestion_block_content_is_byte_exact`, and `test_suggestion_exactly_30_lines_is_committable` FAIL on `assert "```suggestion" in text` (current code renders a plain ` ``` ` fence); `test_suggestion_toggle_off_renders_plain_block` and `test_inline_payload_carries_committable_block_and_honors_toggle` FAIL with `TypeError: ... unexpected keyword argument 'commit_suggestions'`. The four absence-asserting guard tests (no-anchor, fence, over-30-lines, whitespace-only) already PASS against the plain rendering — they exist as regression guards on the new committable path and stay green throughout. The pre-existing `test_inline_comment_omits_suggestion_block_when_none` and `..._includes_suggestion_block_when_provided` still PASS.

- [ ] **Step 3: Implement the guard + rendering**

In `reva/review_formatter.py`, replace `format_inline_comment` and the `body=` line of `format_inline_comment_payload` (lines 308-351) with:

```python
# GitHub renders a ```suggestion fence inside an inline review comment as a
# committable diff replacing EXACTLY the commented line range. Cap the plausible
# size of a single-range replacement; anything larger reads as a rewrite, not a
# line fix, and downgrades to plain text.
MAX_SUGGESTION_LINES = 30


def _is_committable_suggestion(finding: Finding, commit_suggestions: bool) -> bool:
    """Deterministic guard for the committable rendering (no model call).

    A ```suggestion block is only correct when the comment is line-anchored and
    the content is a complete replacement for that range. Downgrading here is a
    rendering rule, NOT a degradation — no ops event; the suggestion is never
    dropped, only rendered as plain text (spec 2026-07-11).
    """
    if not commit_suggestions:
        return False
    if not finding.suggestion or not finding.suggestion.strip():
        return False
    if finding.line_start is None:
        return False  # no anchor — GitHub can't apply a suggestion without a line range
    if "```" in finding.suggestion:
        return False  # a nested fence would terminate the suggestion block early
    if finding.suggestion.rstrip("\n").count("\n") + 1 > MAX_SUGGESTION_LINES:
        return False  # implausibly large for a line replacement — keep as prose
    return True


def format_inline_comment(finding: Finding, commit_suggestions: bool = True) -> str:
    emoji = SEVERITY_EMOJI[finding.severity]
    lines = [
        f"### {emoji} {_SEVERITY_LABEL[finding.severity]}: {finding.title}",
        "",
        f"**Confidence**: {finding.confidence:.2f}",
        f"**Category**: {finding.category}",
        "",
        finding.body,
    ]
    if finding.suggestion:
        if _is_committable_suggestion(finding, commit_suggestions):
            # GitHub is whitespace-sensitive inside the fence: every line is a
            # replacement line for the commented range. Strip trailing newlines
            # (an empty trailing line would otherwise be committed) but keep
            # all indentation and internal blank lines byte-exact.
            lines.extend(
                [
                    "",
                    "**Suggestion**:",
                    "```suggestion",
                    finding.suggestion.rstrip("\n"),
                    "```",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "**Suggestion**:",
                    "```",
                    finding.suggestion,
                    "```",
                ]
            )
    return "\n".join(lines)


def format_inline_comment_payload(finding: Finding, commit_suggestions: bool = True) -> dict:
    """Shape the dict the GitHub Reviews API expects in `comments[]`.

    Requires `line_start` — an inline comment must anchor to a line. Callers
    route line-less findings to the review body (see `split_findings`); enforce
    the invariant here so a stray None fails loudly instead of building an
    invalid payload / TypeError on the line-range check (CORR-20).
    """
    if finding.line_start is None:
        raise ValueError("inline comment requires line_start; route via split_findings")
    payload: dict = {
        "path": finding.file,
        "line": finding.line_start,
        "side": "RIGHT",
        "body": format_inline_comment(finding, commit_suggestions=commit_suggestions),
    }
    if finding.line_end is not None and finding.line_end > finding.line_start:
        payload["start_line"] = finding.line_start
        payload["start_side"] = "RIGHT"
        payload["line"] = finding.line_end
    return payload
```

(The `if finding.line_end is not None ...` block, the docstring, and the `raise` are unchanged from the current file — only the `body=` line and the two new definitions above `format_inline_comment` change. The plain-text `else` branch is byte-identical to today's rendering: no `rstrip` there.)

- [ ] **Step 4: Run the formatter suite**

Run: `cd worker && .venv/bin/python -m pytest tests/test_review_formatter.py -v`
Expected: all PASS, including the pre-existing suggestion tests (`test_inline_comment_includes_suggestion_block_when_provided` now takes the committable path — its assertions on `**Suggestion**` and content still hold).

- [ ] **Step 5: Commit**

```bash
git add reva/review_formatter.py worker/tests/test_review_formatter.py
git commit -m 'feat(formatter): committable GitHub suggestion blocks with deterministic downgrade-to-text guard'
```

---

### Task 2: `commit_suggestions` toggle — RepoConfig → ReviewResult → runner + README row

**Files:**
- Modify: `reva/types.py:74-76` (`RepoConfig`, after `verify_findings`) and `reva/types.py:207-210` (`ReviewResult`, after `block_on_severity`)
- Modify: `worker/worker/reviewer.py:850` (the `ReviewResult(...)` construction in `execute`)
- Modify: `worker/worker/runner.py:594` (`_post_completed_review`)
- Modify: `README.md:133` (config table, after the `verify_findings` row)
- Test: `worker/tests/test_reviewer.py` (after `test_invalid_block_on_severity_falls_back_to_major`, line 553), `worker/tests/test_runner.py` (`_completed_result` at 219, `_f` at 242, new tests after `test_completed_run_posts_check_and_review`, line 284)

**Interfaces:**
- Consumes: `format_inline_comment_payload(finding, commit_suggestions: bool = True)` from Task 1.
- Produces: `RepoConfig.commit_suggestions: bool = True` (parsed from `.claude-review.yml`; unknown-key repos unaffected — `extra="ignore"`), `ReviewResult.commit_suggestions: bool = True` (transient, not persisted).

- [ ] **Step 1: Write the failing tests**

In `worker/tests/test_reviewer.py`, after `test_invalid_block_on_severity_falls_back_to_major` (line 553), mirroring the `block_on_severity` toggle tests directly above:

```python
def test_commit_suggestions_resolved_from_yml():
    github = FakeGitHub(file_contents={".claude-review.yml": "commit_suggestions: false\n"})
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, *_ = _make_reviewer(github=github, runner=runner)
    result = reviewer.execute(_params())
    assert result.status == "completed"
    assert result.commit_suggestions is False


def test_commit_suggestions_defaults_to_true():
    github = FakeGitHub(file_contents={})  # no .claude-review.yml
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, *_ = _make_reviewer(github=github, runner=runner)
    result = reviewer.execute(_params())
    assert result.commit_suggestions is True
```

In `worker/tests/test_runner.py`: extend the two helpers (lines 219-253) — add `commit_suggestions: bool = True` to `_completed_result`'s signature and pass `commit_suggestions=commit_suggestions,` in its `ReviewResult(...)` call (after `block_on_severity=block_on_severity,`); add `suggestion=None` to `_f`'s keyword args and pass `suggestion=suggestion,` in its `Finding(...)` call. Then add, after `test_completed_run_posts_check_and_review` (line 284) — this is the spec's end-to-end test: the **posted** inline comment body carries the fenced block (finding at line 12 maps into `SAMPLE_DIFF`'s hunk):

```python
def test_posted_inline_comment_carries_committable_suggestion(ctx_and_fakes):
    s = ctx_and_fakes
    s["reviewer"].result = _completed_result(
        findings=[_f("major", file="x.py", line_start=12, suggestion="new1_fixed")]
    )

    run_review(_params(s))

    review = s["github"].created_pr_reviews[0]
    assert len(review["comments"]) == 1
    assert "```suggestion\nnew1_fixed\n```" in review["comments"][0]["body"]


def test_toggle_off_posts_plain_text_suggestion(ctx_and_fakes):
    s = ctx_and_fakes
    s["reviewer"].result = _completed_result(
        findings=[_f("major", file="x.py", line_start=12, suggestion="new1_fixed")],
        commit_suggestions=False,
    )

    run_review(_params(s))

    body = s["github"].created_pr_reviews[0]["comments"][0]["body"]
    assert "```suggestion" not in body
    assert "```\nnew1_fixed\n```" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_reviewer.py -k commit_suggestions -v tests/test_runner.py -k "committable_suggestion or toggle_off" -v`
Expected: the two reviewer tests FAIL with `AttributeError: 'ReviewResult' object has no attribute 'commit_suggestions'` (`ReviewResult` has no model_config, so pydantic's default `extra="ignore"` silently drops the unknown `commit_suggestions=` kwarg the Step-1 helper edit now passes — reading it back then raises). `test_posted_inline_comment_carries_committable_suggestion` already PASSES (Task 1's parameter default is `True`); `test_toggle_off_posts_plain_text_suggestion` FAILS on `assert "```suggestion" not in body` — the runner ignores the toggle until Step 3 wires it, which is exactly the missing plumbing this test proves.

- [ ] **Step 3: Implement the plumbing**

`reva/types.py` — in `RepoConfig`, directly after the `verify_findings` field (line 76):

```python
    # Render line-anchored suggestions as committable GitHub ```suggestion
    # blocks ("Commit suggestion" button on the PR). false = the previous
    # plain-text rendering everywhere (spec 2026-07-11).
    commit_suggestions: bool = True
```

`reva/types.py` — in `ReviewResult`, directly after the `block_on_severity` field (line 210):

```python
    # Transient: per-repo committable-suggestions toggle, carried from
    # Reviewer.execute to the inline-comment formatter (same pattern as
    # block_on_severity). Not persisted; True matches the RepoConfig default
    # for results built outside execute.
    commit_suggestions: bool = True
```

`worker/worker/reviewer.py` — in the `ReviewResult(...)` construction in `execute`, directly after `block_on_severity=repo_config.block_on_severity,` (line 850):

```python
            commit_suggestions=repo_config.commit_suggestions,
```

`worker/worker/runner.py` — in `_post_completed_review`, replace line 594:

```python
    comments = [format_inline_comment_payload(f) for f in inline]
```

with:

```python
    comments = [
        format_inline_comment_payload(f, commit_suggestions=result.commit_suggestions)
        for f in inline
    ]
```

`README.md` — in the per-repo config table, after the `verify_findings` row (line 133), add:

```markdown
| `commit_suggestions` | `true` | Render line-anchored suggestions as committable GitHub suggestion blocks ("Commit suggestion" button); `false` keeps plain text. |
```

- [ ] **Step 4: Run the covering suites**

Run: `cd worker && .venv/bin/python -m pytest tests/test_reviewer.py tests/test_runner.py tests/test_review_formatter.py -q`
Expected: all PASS (existing tests unaffected — the new fields default to today's behavior).

- [ ] **Step 5: Commit**

```bash
git add reva/types.py worker/worker/reviewer.py worker/worker/runner.py README.md worker/tests/test_reviewer.py worker/tests/test_runner.py
git commit -m 'feat(review): per-repo commit_suggestions toggle, reviewer -> runner -> formatter'
```

---

### Task 3: Prompt contract — exact-replacement rule in the five skills + CHANGELOG v2.9

**Files:**
- Modify: `prompts/skills/reva-diff-review.md` (sample line 56, bullet line 67)
- Modify: `prompts/skills/reva-full-review.md` (sample line 79, bullet lines 90-92)
- Modify: `prompts/skills/reva-xml-review.md` (sample line 57, bullet line 68)
- Modify: `prompts/skills/reva-migration-review.md` (sample line 72, bullet line 83)
- Modify: `prompts/skills/reva-delta-review.md` (sample line 79, bullet line 87)
- Modify: `prompts/CHANGELOG.md` (line 1 — wave-shared v2.9 entry, see Global Constraints)
- Test: `worker/tests/test_prompt_files.py` (new test after `test_intent_check_in_skill_output_contracts`, line 137; version assert at line 47)
- Do NOT touch: `prompts/skills/reva-repo-audit.md` (audit findings become issues, not inline comments).

**Interfaces:**
- Consumes: nothing from earlier tasks (prompt files are read raw by `reva/claude_code_runner.py`; version parsed by `PromptBuilder.get_version` from the first `##` heading of `CHANGELOG.md`, `reva/prompt_builder.py:23-34`).
- Produces: skill text containing the literal phrases `exact replacement` and ` ```suggestion ` (asserted by the new prompt-content test).

- [ ] **Step 1: Write the failing test**

In `worker/tests/test_prompt_files.py`, after `test_intent_check_in_skill_output_contracts` (line 137):

```python
def test_committable_suggestion_rule_in_skill_output_contracts():
    # Committable-suggestions spec (2026-07-11): the five inline-review skills
    # must state the exact-replacement rule for `suggestion` — it is rendered
    # as a GitHub ```suggestion block replacing exactly the commented line
    # range. The audit skill is excluded: audit findings become GitHub issues,
    # not inline comments.
    for skill in (
        "reva-diff-review.md", "reva-full-review.md", "reva-xml-review.md",
        "reva-migration-review.md", "reva-delta-review.md",
    ):
        text = (SKILLS_DIR / skill).read_text()
        assert "exact replacement" in text, skill
        assert "```suggestion" in text, skill
    assert "```suggestion" not in (SKILLS_DIR / "reva-repo-audit.md").read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && .venv/bin/python -m pytest tests/test_prompt_files.py -k committable -v`
Expected: FAIL — `AssertionError: reva-diff-review.md` (no skill mentions `exact replacement` yet).

- [ ] **Step 3: Edit the five skill files**

In **each** of the five files, make the same two edits:

(a) Replace the JSON-sample line (identical in all five):

```json
      "suggestion": "Concrete fix, or null",
```

with:

```json
      "suggestion": "Exact replacement for lines line_start..line_end, or null",
```

(b) Insert a new bullet **immediately after** the "may be `null`" bullet. For `reva-diff-review.md` (line 67), `reva-xml-review.md` (line 68), `reva-migration-review.md` (line 83), and `reva-delta-review.md` (line 87) that anchor bullet is the single line:

```markdown
- `file`, `line_start`, `line_end`, `suggestion` may be `null`.
```

For `reva-full-review.md` (lines 90-92) it is the three-line variant:

```markdown
- `file`, `line_start`, `line_end`, `suggestion` may be `null`. Prefer a line
  that maps to the diff so the finding can be posted inline; otherwise leave
  `file`/`line_start` null and it appears in the summary.
```

The inserted bullet (identical in all five files):

```markdown
- `suggestion` must be the **exact replacement** for lines `line_start`..`line_end`
  on the new side: complete lines, matching indentation, no prose, no ellipses,
  no markdown fences. It is posted as a GitHub `` ```suggestion `` block — one
  click commits it over exactly those lines. If the fix isn't expressible as a
  line replacement (multi-file, conceptual, larger scope), set `suggestion` to
  `null` and put the advice in `body`.
```

- [ ] **Step 4: CHANGELOG — the wave-shared v2.9 entry (conditional)**

Check the current top heading:

```bash
head -1 prompts/CHANGELOG.md
```

**If it prints `## v2.8 — Issue-conformance verdicts`** (no sibling wave plan has landed yet): insert the following ABOVE it, and in `worker/tests/test_prompt_files.py:47` change `assert builder.get_version() == "v2.8"` to `assert builder.get_version() == "v2.9"`:

```markdown
## v2.9 — 2026-07 feature wave

- reva-diff-review.md / reva-full-review.md / reva-xml-review.md /
  reva-migration-review.md / reva-delta-review.md: `suggestion` output
  contract tightened for committable GitHub suggestion blocks — must be the
  exact replacement for `line_start`..`line_end` (complete lines, matching
  indentation, no prose, no ellipses, no fences), or `null` with the advice
  in `body`. Rendered as a one-click "Commit suggestion" on the PR
  (`commit_suggestions` repo toggle, default on).

```

**If it already prints a `## v2.9 — ...` heading** (a sibling plan created it): append ONLY the bullet above (the `- reva-diff-review.md / ...` list item, without the heading) to the existing v2.9 entry's bullet list, and verify `worker/tests/test_prompt_files.py:47` already asserts `"v2.9"` — leave it alone. Never create a v2.10 heading.

- [ ] **Step 5: Run the prompt suite**

Run: `cd worker && .venv/bin/python -m pytest tests/test_prompt_files.py -v`
Expected: all PASS, including `test_get_version_returns_current_version` (now v2.9) and the new `test_committable_suggestion_rule_in_skill_output_contracts`.

- [ ] **Step 6: Commit**

```bash
git add prompts/skills/reva-diff-review.md prompts/skills/reva-full-review.md prompts/skills/reva-xml-review.md prompts/skills/reva-migration-review.md prompts/skills/reva-delta-review.md prompts/CHANGELOG.md worker/tests/test_prompt_files.py
git commit -m 'feat(prompts): exact-replacement suggestion contract in the five review skills (v2.9)'
```

---

### Task 4: Verification sweep + spec sync

**Files:**
- Modify: `docs/superpowers/specs/2026-07-11-committable-suggestions-design.md` (Status line at line 4; `description` → `body` wording in Design item 2, line 29)

- [ ] **Step 1: Full gates** (shared `reva/` touched → all three services)

Run from the repo root:

```bash
make test
worker/.venv/bin/ruff check reva worker/worker api/app scheduler/scheduler
```

Expected: worker, api, and scheduler suites all green; ruff clean (no output, exit 0). Advisory (non-blocking): `mypy reva worker/worker api/app scheduler/scheduler --ignore-missing-imports` shows no NEW errors vs. main. No `tui/` changes were made, so no Go gate is required — confirm with `git status --porcelain tui/` printing nothing.

- [ ] **Step 2: Spec sync**

In `docs/superpowers/specs/2026-07-11-committable-suggestions-design.md`:

Replace line 4:

```markdown
- **Status:** approved (design), not yet planned
```

with:

```markdown
- **Status:** approved — implemented; see `plans/2026-07-11-committable-suggestions.md`.
```

And in Design item 2 (line 29), correct the field name — replace:

```markdown
   (multi-file, conceptual, or larger-scope advice). Prose advice belongs in
   `description`.
```

with:

```markdown
   (multi-file, conceptual, or larger-scope advice). Prose advice belongs in
   `body` (the spec originally said `description`; the Finding field is `body`).
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-07-11-committable-suggestions-design.md
git commit -m 'docs(specs): committable suggestions -- mark implemented, correct field name'
```

**Honest-status note for the final report:** all coverage is unit-level (pure formatter tests, SQLite + fake-GitHub runner tests, prompt-content greps) — no live GitHub round-trip. The spec's staging gate remains open: one live review on a linked PR, click "Commit suggestion" once and confirm GitHub applies exactly the commented range. Prompt-contract adherence (models actually emitting exact-replacement suggestions) is only verifiable on live reviews.
