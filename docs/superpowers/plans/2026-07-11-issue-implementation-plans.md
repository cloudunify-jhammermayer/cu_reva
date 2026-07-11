# Implementation Plans in Generated Issues — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a create-issues run completes (issues persisted AND the `issues-created` Odoo callback sent), REVA researches the repo clone with one deep-model headless-CLI session and appends a marked, idempotently-replaced "Implementation plan" block to each created issue's body — gated by per-repo `issue_plans: false` (default OFF) AND the global `REVA_ISSUE_PLANS_ENABLED` env switch (default on), fail-soft like the board-projection step.

**Spec:** `docs/superpowers/specs/2026-07-11-issue-implementation-plans-design.md` (approved 2026-07-11).

**Architecture:** The step is a new module-level `_issue_plans_step(ctx, params, issues, log)` in `worker/worker/ticket_issue_runner.py`, invoked from `run_ticket_issues` **after** the `odoo.issues_created` try/except block (its `except PermanentError` handler ends at line 436) and **before** `log.info("ticket_issues_done", ...)` at line 438 — a `TransientError` on the callback re-raises before the step, so plans only ever run on an attempt whose callback succeeded (or 4xx-completed); the RQ retry re-sends the callback first and then plans. Clone machinery is the existing `ClaudeCodeRunner`: `ctx.runner.repo_lock(owner, repo)` + `ctx.runner.ensure_repo(owner, repo, None, token)` — `head_sha=None` hard-resets to `origin/HEAD`, i.e. the default-branch head, exactly the audit path (`worker/worker/auditor.py:58-67`) — then `ctx.runner.review(repo_path, skill="reva-issue-plans", params=..., model=ctx.runner.deep_model, odoo=repo_config.odoo, extra_dirs=core_paths)` with the temp-JSON output contract `{"plans": [{"number", "plan_markdown"}]}`. Core-knowledge worktrees ride `extra_dirs` + a `core_knowledge` skill param (the reviewer's pattern, `worker/worker/reviewer.py:682-706`); CodeGraph rides the `_CODEGRAPH_SKILLS` frozenset (`reva/claude_code_runner.py:60`), which gains `"reva-issue-plans"`. Issue bodies are edited via a **new** `GitHubClient.update_issue` (PATCH; the client has `get_issue` and a `_patch` helper but no issue-body writer today). Plan cost folds into the run row's existing `estimated_cost_usd` column via a new `writers.add_ticket_issue_run_cost` — **no migration, no new column**. **Spec corrections discovered during planning:** (1) skills in `prompts/skills/` are FLAT `.md` files, not directories — the spec's `prompts/skills/reva-issue-plans/` must read `prompts/skills/reva-issue-plans.md`; Task 5 corrects the spec text. (2) The skill file follows the house style of the existing flat skills (starts with `## Task:`, no frontmatter — see `reva-repo-audit.md`).

**Tech Stack:** Python 3.14, pydantic 2, headless Claude Code CLI (`reva/claude_code_runner.py`), pytest per-service venvs, markdown skill files under `prompts/skills/`. No new dependencies, no DB migrations, no contract changes, no TUI changes.

## Global Constraints

- **RepoConfig wave coordination:** `reva/types.py` `RepoConfig`: THIS plan adds ONLY `issue_plans: bool = False` (**OFF by default — locked decision**) + its README config-table row. Siblings add `commit_suggestions`, `describe_command`, `fix_command` — merge-trivial, don't touch theirs.
- **Env addition (THIS plan only):** `REVA_ISSUE_PLANS_ENABLED` default `true` in `reva/config.py` + README env-table row + `.env.example` row (`worker/tests/test_env_example.py` gates: every `REVA_*` var read in `reva/config.py` must appear in `.env.example`). Effective = env AND repo config; switch-skips log at **debug**, **NO ops event** (a switch doing its job is not a degradation — spec states this).
- **prompts/CHANGELOG.md:** top entry is currently v2.8 (unshipped). Wave rule: ALL five features share ONE **v2.9** entry — create the v2.9 entry if absent, otherwise append a bullet to the existing v2.9. **Never mint v2.10.** Coupling: `worker/tests/test_prompt_files.py:47` asserts the parsed version string — whichever plan creates v2.9 updates that assertion to `"v2.9"`; if it already says v2.9, leave it.
- **No changes to `reva/odoo_contracts.py` or `contracts/`** (spec: zero contract impact — plans live only in GitHub issue bodies; the Odoo callback flow completes before this step runs).
- **Ops-event invariant (CLAUDE.md):** every caught-and-degraded path (CLI failure, malformed output, GitHub edit failure, budget skip) must log AND `writers.record_ops_event(...)` with `component="issue_plans"`; the run stays `completed` regardless. The callback-BEFORE-plans ordering must be asserted by a test.
- **Locked decisions to encode verbatim:** ONE CLI session per run on `REVA_DEEP_MODEL` (not per-issue sessions); the marked block is replaced idempotently on requeued/resumed runs; hallucinated/omitted issue numbers are skipped with a warning log; plan block header is exactly `## 🤖 Implementation plan (AI-generated — verify before following)`.
- **No DB migration:** plan cost is added into the run row's existing `estimated_cost_usd` (the spec's "persisted on the run row alongside the planning cost").
- **No `tui/` changes in this plan.**
- **Final verification:** `make test` green (shared `reva/` touched → worker, api, scheduler), `worker/.venv/bin/ruff check reva worker/worker api/app scheduler/scheduler` clean.
- Per-service venvs: `cd worker && .venv/bin/python -m pytest tests/...`.

---

### Task 0: Preconditions

**Files:** none (checks only).

- [ ] **Step 1: Verify a clean tree at/after the wave-plans commit**

Run from `/home/joseph/Projects/Cloudunify/cu_reva`:

```bash
git status --porcelain
git merge-base --is-ancestor 9079f99 HEAD && echo ANCESTOR_OK
test -f docs/superpowers/specs/2026-07-11-issue-implementation-plans-design.md && echo SPEC_OK
grep -n "issue_plans" reva/types.py || echo NO_FIELD_YET
```

Expected: empty output from `git status --porcelain` (sibling wave plans may already be merged — that is fine, only *uncommitted* changes block), then `ANCESTOR_OK`, then `SPEC_OK`, then `NO_FIELD_YET` (no sibling adds `issue_plans`; a hit means this plan already ran — STOP and reconcile). If any check fails, STOP and report — do not proceed on a dirty tree or a stale base.

---

### Task 1: Toggles — `issue_plans_enabled()` env switch + `RepoConfig.issue_plans` + doc rows

**Files:**
- Modify: `reva/config.py` (append after `required_env_or_file`, line 47)
- Modify: `reva/types.py` (`RepoConfig`, append after `odoo_version: str | None = None`, line 99)
- Modify: `README.md` (config table — add a row after the `odoo / custom_instructions` row, line 134; env table — add a row after the `REVA_DEEP_MODEL` row, line 264)
- Modify: `.env.example` (after the CodeGraph block, ~line 66)
- Test: `worker/tests/test_config.py` (append)

**Interfaces:**
- Produces (Task 4 consumes):
  - `reva.config.issue_plans_enabled() -> bool` — reads `REVA_ISSUE_PLANS_ENABLED` at **call time** (monkeypatch-friendly; the module-constant pattern of `DEFAULT_MODEL` binds at import and can't be flipped per-test).
  - `RepoConfig.issue_plans: bool = False` — parsed from `.claude-review.yml` (`extra="ignore"` keeps unknown-key repos unaffected).

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_config.py`:

```python
# --- issue_plans_enabled (implementation-plans global kill switch) ------------


def test_issue_plans_enabled_defaults_true(monkeypatch):
    monkeypatch.delenv("REVA_ISSUE_PLANS_ENABLED", raising=False)
    from reva.config import issue_plans_enabled
    assert issue_plans_enabled() is True


def test_issue_plans_enabled_env_off(monkeypatch):
    from reva.config import issue_plans_enabled
    for v in ("false", "0", "no", "FALSE"):
        monkeypatch.setenv("REVA_ISSUE_PLANS_ENABLED", v)
        assert issue_plans_enabled() is False


def test_issue_plans_enabled_truthy_forms(monkeypatch):
    from reva.config import issue_plans_enabled
    for v in ("1", "true", "yes", "TRUE"):
        monkeypatch.setenv("REVA_ISSUE_PLANS_ENABLED", v)
        assert issue_plans_enabled() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_config.py -k issue_plans -v`
Expected: 3 FAIL with `ImportError: cannot import name 'issue_plans_enabled' from 'reva.config'`.

- [ ] **Step 3: Implement**

`reva/config.py` — append at the end of the file (after `required_env_or_file`):

```python
def issue_plans_enabled() -> bool:
    """REVA_ISSUE_PLANS_ENABLED (default true): global kill switch for the
    implementation-plans step on REVA-generated ticket issues. `false` disables
    the step everywhere regardless of per-repo `.claude-review.yml`
    `issue_plans` (operator kill switch — spec 2026-07-11). Read at call time
    so tests and live env flips take effect without a re-import.
    """
    return os.environ.get("REVA_ISSUE_PLANS_ENABLED", "true").lower() in ("1", "true", "yes")
```

`reva/types.py` — in `RepoConfig`, directly after `odoo_version: str | None = None` (line 99, end of the class):

```python
    # Per-repo opt-in for AI-generated implementation plans appended to
    # REVA-created ticket issues (spec 2026-07-11). OFF by default (locked
    # decision) — each opted-in create-issues run pays one deep-model CLI
    # session. The global REVA_ISSUE_PLANS_ENABLED switch must also be on.
    issue_plans: bool = False
```

`README.md` — config table, add after the `odoo / custom_instructions` row (line 134):

```markdown
| `issue_plans` | `false` | Append an AI-generated implementation plan to each REVA-created ticket issue (deep model, grounded in the repo clone). `REVA_ISSUE_PLANS_ENABLED` must also be `true`. |
```

`README.md` — env table, add after the `REVA_DEEP_MODEL` row (line 264):

```markdown
| `REVA_ISSUE_PLANS_ENABLED` | — | `true` | Global kill switch for implementation plans on generated issues; repos still opt in via `.claude-review.yml` `issue_plans: true` |
```

`.env.example` — add after the CodeGraph block (~line 66):

```bash
# Implementation plans appended to REVA-created ticket issues (deep model,
# grounded in the repo clone). Global kill switch — repos additionally opt in
# via .claude-review.yml `issue_plans: true` (default off per repo).
# REVA_ISSUE_PLANS_ENABLED=true
```

- [ ] **Step 4: Run the covering suites**

```bash
cd worker && .venv/bin/python -m pytest tests/test_config.py tests/test_env_example.py -q
.venv/bin/python -c "from reva.types import RepoConfig; assert RepoConfig().issue_plans is False; assert RepoConfig.model_validate({'issue_plans': True}).issue_plans is True; print('REPOCONFIG_OK')"
```

Expected: all PASS (`test_env_example` proves the `.env.example` row landed), then `REPOCONFIG_OK`.

- [ ] **Step 5: Commit**

```bash
git add reva/config.py reva/types.py README.md .env.example worker/tests/test_config.py
git commit -m 'feat(config): issue_plans repo toggle (default off) + REVA_ISSUE_PLANS_ENABLED global switch'
```

---

### Task 2: GitHub client — `update_issue` (PATCH issue body)

**Files:**
- Modify: `reva/github_client.py` (add after `get_issue`, which ends at line 273; uses the existing `_patch` helper at line 1027 — the `update_check_run` idiom, lines 454-479)
- Test: `worker/tests/test_github_client.py` (append after `test_update_check_run_patches_existing`, line 475; uses the file's `rsa_key_pair` fixture and `_make_client` helper, lines 26-52)

**Interfaces:**
- Produces (Task 4 consumes): `GitHubClient.update_issue(token: str, owner: str, repo: str, issue_number: int, body: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_github_client.py` after `test_update_check_run_patches_existing` (line 475):

```python
def test_update_issue_patches_body_only(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"number": 42})

    client = _make_client(handler, private_pem)
    client.update_issue("tok", "acme", "widgets", 42, body="new body\n<!-- reva:impl-plan -->…")

    assert captured["method"] == "PATCH"
    assert captured["path"] == "/repos/acme/widgets/issues/42"
    assert captured["body"] == {"body": "new body\n<!-- reva:impl-plan -->…"}  # body-only PATCH


def test_update_issue_raises_on_error_status(rsa_key_pair):
    private_pem, _ = rsa_key_pair

    def handler(req):
        return httpx.Response(403, json={"message": "Resource not accessible"})

    client = _make_client(handler, private_pem)
    with pytest.raises(PermanentError):
        client.update_issue("tok", "acme", "widgets", 42, body="x")
```

(`json`, `httpx`, `pytest`, and `PermanentError` are already imported at the top of the file.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_github_client.py -k update_issue -v`
Expected: 2 FAIL with `AttributeError: 'GitHubClient' object has no attribute 'update_issue'`.

- [ ] **Step 3: Implement**

`reva/github_client.py` — add directly after `get_issue` (after line 273):

```python
    def update_issue(
        self, token: str, owner: str, repo: str, issue_number: int, body: str
    ) -> None:
        """PATCH an issue's body (implementation-plans block upsert). Body-only
        on purpose: REVA never rewrites titles/labels/assignees here, and the
        caller sends the FULL new body — the marked-block replace/append happens
        caller-side. Same Issues write scope as create_issue, so no new GitHub
        App permission."""
        self._patch(
            token, f"/repos/{owner}/{repo}/issues/{issue_number}", {"body": body}
        )
```

- [ ] **Step 4: Run the client suite**

Run: `cd worker && .venv/bin/python -m pytest tests/test_github_client.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add reva/github_client.py worker/tests/test_github_client.py
git commit -m 'feat(github): update_issue — body-only PATCH for the implementation-plans block'
```

---

### Task 3: `reva-issue-plans` skill file + CodeGraph wiring + CHANGELOG v2.9

**Files:**
- Create: `prompts/skills/reva-issue-plans.md` (flat `.md` file — the spec's `reva-issue-plans/` directory form is wrong, skills are flat; corrected in Task 5)
- Modify: `reva/claude_code_runner.py` (`_CODEGRAPH_SKILLS` + its comment, lines 57-60)
- Modify: `prompts/skills/README.md` (skill table — add a row after the `reva-repo-audit.md` row)
- Modify: `README.md` (CodeGraph section, line 154 — "full/deep PR reviews and repo audits" gains issue plans)
- Modify: `prompts/CHANGELOG.md` (line 1 — wave-shared v2.9 entry, see Global Constraints)
- Test: `worker/tests/test_prompt_files.py` (new test after `test_intent_check_in_skill_output_contracts`, line 137; `test_summary_contract_present` at 140-145 gains a skip; version assert at line 47 — conditional), `worker/tests/test_claude_code_runner.py` (`cg_runner` fixture skill list at line 983, parametrize at line 1026)

**Interfaces:**
- Consumes: `ClaudeCodeRunner._read_skill` resolves `skills_dir/reva-issue-plans.md` (`reva/claude_code_runner.py:563-569`) — nothing to change there, flat files are the mechanism.
- Produces (Task 4 consumes at runtime): the skill's output contract — Claude Writes `{"plans": [{"number": int, "plan_markdown": str}]}` to `output_path`; CodeGraph MCP now engages for `skill="reva-issue-plans"` when `REVA_CODEGRAPH_ENABLED=true`.

- [ ] **Step 1: Write the failing tests**

In `worker/tests/test_prompt_files.py`, after `test_intent_check_in_skill_output_contracts` (line 137):

```python
def test_issue_plans_skill_present_and_well_formed():
    # Implementation-plans spec (2026-07-11): flat skill file, not a directory.
    text = (SKILLS_DIR / "reva-issue-plans.md").read_text()
    assert text.strip(), "reva-issue-plans.md is empty"
    assert "output_path" in text          # temp-JSON output contract
    assert "plan_markdown" in text        # {plans: [{number, plan_markdown}]}
    assert '"number"' in text
    assert "not** a code review" in text  # it must not emit findings
```

And in `test_summary_contract_present` (lines 140-145), the `for skill in SKILLS_DIR.glob("reva-*.md")` loop now needs to skip the new non-review skill — replace the loop body:

```python
    # every skill's summary placeholder points reviewers at the contract
    for skill in SKILLS_DIR.glob("reva-*.md"):
        if skill.name == "reva-issue-plans.md":
            continue  # not a review skill: its contract has no summary field
        assert "Summary contract" in skill.read_text(), skill.name
```

In `worker/tests/test_claude_code_runner.py`: extend the CodeGraph gating —

(a) `cg_runner` fixture (line 983), add the new skill to the tuple:

```python
    for name in ("reva-full-review", "reva-repo-audit", "reva-diff-review",
                 "reva-delta-review", "reva-issue-plans"):
```

(b) parametrize on `test_review_engages_codegraph_for_repo_aware_skills` (line 1026):

```python
@pytest.mark.parametrize("skill", ["reva-full-review", "reva-repo-audit", "reva-issue-plans"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_prompt_files.py -k issue_plans -v tests/test_claude_code_runner.py -k codegraph -v`
Expected: `test_issue_plans_skill_present_and_well_formed` FAILS (`FileNotFoundError`); the new `reva-issue-plans` parametrization of `test_review_engages_codegraph_for_repo_aware_skills` FAILS on `assert "--mcp-config" in argv` (skill not in `_CODEGRAPH_SKILLS` yet). The two pre-existing parametrizations still PASS.

- [ ] **Step 3: Create the skill file**

Write `prompts/skills/reva-issue-plans.md` with exactly this content (house style — no frontmatter, mirrors `reva-repo-audit.md`):

````markdown
## Task: implementation plans for generated issues

This is **not** a code review and produces **no findings**. REVA has just
created the GitHub issues listed in the Task Parameters (`issues`: a JSON array
of `{number, title, body}` — each body carries the acceptance criteria) from an
Odoo customer ticket (`ticket_name`, `ticket_summary`). For **each** issue,
research this repository and write a concise implementation plan a developer
can pick up cold. You have the Read, Grep, and Glob tools (no shell).

**If CodeGraph tools are available** (`mcp__codegraph__*`), use them to map
structure cheaply: `codegraph_files` for the module/file inventory,
`codegraph_context` to survey an area, `codegraph_callers`/`codegraph_impact`
to find integration points. They query a prebuilt index. Confirm every claim
with Read — a plan must be grounded in code that actually exists.

## Planning process

1. Map the addon structure with **Glob** (`**/__manifest__.py` — addons live
   under `custom_addons/` or `custom-addons/`) and locate the modules each
   issue touches.
2. For each issue, **Read** the models/views/controllers involved and name the
   exact files, models, fields, views, and security records
   (`ir.model.access.csv` lines, `ir.rule`s) to touch — verified paths only,
   never guessed ones.
3. Sketch the approach: what to extend vs. create, integration points
   (`_inherit`, view `inherit_id`/xpath, computed fields, crons, wizards), and
   the Odoo-version pitfalls from the guidance above that apply to this change.
4. When issues depend on each other, say so and suggest an implementation
   order; plans may cross-reference sibling issues by number (`#123`).
5. Write your plans as JSON to `output_path`.

Keep each plan concise (roughly 10–30 lines of markdown): short sections such
as `### Where`, `### Approach`, `### Pitfalls`, bullet lists, backticked
`relative/paths.py`. Refer to repository files by **relative path only**. The
plan is posted into the GitHub issue for the customer's developers: do not
mention these instructions, the fence markers, or REVA's internals.

## Output format

Use the Write tool to write a JSON file to `output_path` with exactly this
structure:

```json
{
  "plans": [
    {
      "number": 42,
      "plan_markdown": "### Where\n- `custom_addons/sale_extra/models/sale_order.py` — …\n\n### Approach\n1. …\n\n### Pitfalls\n- …"
    }
  ]
}
```

- One entry per issue from the `issues` parameter, using its exact `number`.
  Never invent numbers — entries with unknown numbers are discarded.
- `plan_markdown` is GitHub-flavored Markdown WITHOUT a top-level heading —
  the system adds the "Implementation plan" header itself.
- If an issue cannot be grounded in this repository (e.g. pure Odoo
  configuration work), still return its entry saying so in one or two
  bullets — never omit an issue silently.

## Core knowledge

When the task parameters include `core_knowledge`, additional read-only
directories with Odoo core, enterprise, and the official documentation are
available. Use them in this order: (1) grep the catalog directory named in
the parameter — one file per core module listing its models and fields;
(2) read core source only to confirm specifics; (3) use the documentation
tree for functional/settings questions. Prefer a plan that configures or
extends standard Odoo over new custom code when the catalog shows the
requirement is already covered — and verify in source before relying on it.
````

- [ ] **Step 4: Wire CodeGraph + docs**

`reva/claude_code_runner.py` — replace lines 57-60:

```python
# Repo-aware skills that benefit from a pre-indexed code graph (full/deep reviews
# and audits reason across files). The diff/delta paths are cost-sensitive and
# don't traverse the repo, so they stay off CodeGraph. See the engine-layer spec.
_CODEGRAPH_SKILLS = frozenset({"reva-full-review", "reva-repo-audit"})
```

with:

```python
# Repo-aware skills that benefit from a pre-indexed code graph (full/deep
# reviews, audits, and issue implementation plans reason across files). The
# diff/delta paths are cost-sensitive and don't traverse the repo, so they stay
# off CodeGraph. See the engine-layer spec.
_CODEGRAPH_SKILLS = frozenset({"reva-full-review", "reva-repo-audit", "reva-issue-plans"})
```

`prompts/skills/README.md` — in the skill table, add after the `reva-repo-audit.md` row:

```markdown
| `reva-issue-plans.md` | create-issues runs on repos with `issue_plans: true` (spec 2026-07-11) | Researches the clone (+ `/core` worktrees) and writes `{plans: [{number, plan_markdown}]}` — one plan per created issue, posted into the issue bodies as a marked block. Not a review: no findings. |
```

`README.md` line 154 — replace the phrase `to the **repo-aware** skills only — full/deep PR reviews and repo audits.` with `to the **repo-aware** skills only — full/deep PR reviews, repo audits, and issue implementation plans.` (rest of the line unchanged).

- [ ] **Step 5: CHANGELOG — the wave-shared v2.9 entry (conditional)**

Check the current top heading:

```bash
head -1 prompts/CHANGELOG.md
```

**If it prints `## v2.8 — Issue-conformance verdicts`** (no sibling wave plan has landed yet): insert the following ABOVE it, and in `worker/tests/test_prompt_files.py:47` change `assert builder.get_version() == "v2.8"` to `assert builder.get_version() == "v2.9"`:

```markdown
## v2.9 — 2026-07 feature wave

- reva-issue-plans.md (new skill): per-issue implementation plans for
  REVA-generated ticket issues — grounded in the repo clone (+ `/core`
  worktrees and CodeGraph when enabled), one deep-model CLI session per
  create-issues run, output `{plans: [{number, plan_markdown}]}`; posted into
  the issue bodies as a marked, idempotently-replaced block (`issue_plans`
  repo toggle, default off; `REVA_ISSUE_PLANS_ENABLED` global switch).

```

**If it already prints a `## v2.9 — ...` heading** (a sibling plan created it): append ONLY the bullet above (the `- reva-issue-plans.md ...` list item, without the heading) to the existing v2.9 entry's bullet list, and verify `worker/tests/test_prompt_files.py:47` already asserts `"v2.9"` — leave it alone. Never create a v2.10 heading.

- [ ] **Step 6: Run the covering suites**

Run: `cd worker && .venv/bin/python -m pytest tests/test_prompt_files.py tests/test_claude_code_runner.py -q`
Expected: all PASS — including `test_get_version_returns_current_version` (now v2.9), `test_summary_contract_present` (with the skip), the new skill-presence test, and all three CodeGraph parametrizations. (Adding a skill file changes the review prompt hash — that is exactly what the v2.9 bump accounts for; the drift alert fires only if the version does NOT change.)

- [ ] **Step 7: Commit**

```bash
git add prompts/skills/reva-issue-plans.md prompts/skills/README.md prompts/CHANGELOG.md reva/claude_code_runner.py README.md worker/tests/test_prompt_files.py worker/tests/test_claude_code_runner.py
git commit -m 'feat(prompts): reva-issue-plans skill — repo-grounded plans for generated issues (v2.9)'
```

---

### Task 4: The pipeline step — cost writer, marker helpers, `_issue_plans_step`, wiring + tests

**Files:**
- Modify: `reva/db/writers.py` (add `add_ticket_issue_run_cost` after `record_ticket_issue_plan`, which ends at line 2041; `update`, `func`, and `TicketIssueRun` are already imported at the top)
- Modify: `worker/worker/ticket_issue_runner.py`:
  - imports (lines 26-41)
  - new module-level markers + helpers + `_load_repo_config` + `_issue_plans_step`, inserted after `_project_step` (ends line 678) and before `_plan_and_create` (line 681)
  - `run_ticket_issues`: one call inserted after the `odoo.issues_created` try/except (after line 436), before `log.info("ticket_issues_done", ...)` (line 438)
- Test: `worker/tests/test_ticket_issue_writers.py` (append), `worker/tests/test_ticket_issue_runner.py` (FakeGitHub 50-157, FakeOdoo 160-199, `ctx_and_fakes` fixture 219-241, one existing assertion at 723-726, new tests appended at the end)

**Interfaces:**
- Consumes: `issue_plans_enabled()` + `RepoConfig.issue_plans` (Task 1), `GitHubClient.update_issue` (Task 2), the `reva-issue-plans` skill + CodeGraph gating (Task 3), and pre-existing: `ClaudeCodeRunner.repo_lock/ensure_repo/review/deep_model`, `CoreKnowledge.resolve/core_paths/catalog_path`, `budget_exceeded(ctx)` (`worker/worker/runner.py:353`), `writers.record_claude_spend`, `writers.record_ops_event`, `reva.cost.estimate_cost`, `reva.review_formatter._redact_internal_paths` (SECU-21 redaction — module-private but the sanctioned posted-text redactor; the /describe sibling reuses it the same way).
- Produces:
  - `writers.add_ticket_issue_run_cost(db: Database, run_id: int, extra_cost_usd: float) -> None`
  - `worker.ticket_issue_runner._issue_plans_step(ctx, params: TicketIssueJobParams, issues: list[dict], log) -> None`
  - module helpers `_upsert_plan_block(body: str, plan_markdown: str) -> str`, `_strip_plan_block(body: str) -> str`, `_load_repo_config(ctx, token: str, owner: str, repo: str) -> RepoConfig`

- [ ] **Step 1: Write the failing writer test**

Append to `worker/tests/test_ticket_issue_writers.py`:

```python
def test_add_ticket_issue_run_cost_accumulates(db):
    # Plans cost folds into the run row's estimated_cost_usd alongside the
    # planning cost (spec 2026-07-11) — NULL-safe, additive, no new column.
    run_id = writers.record_ticket_issue_run_created(db, _typed_params(ticket_id=95))
    writers.add_ticket_issue_run_cost(db, run_id, 1.25)   # NULL -> 1.25
    assert writers.get_ticket_issue_run(db, run_id)["estimated_cost_usd"] == pytest.approx(1.25)
    writers.add_ticket_issue_run_cost(db, run_id, 0.5)    # 1.25 -> 1.75
    assert writers.get_ticket_issue_run(db, run_id)["estimated_cost_usd"] == pytest.approx(1.75)
```

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_writers.py -k run_cost -v`
Expected: FAIL — `AttributeError: module 'reva.db.writers' has no attribute 'add_ticket_issue_run_cost'`.

- [ ] **Step 2: Implement the writer**

`reva/db/writers.py` — add after `record_ticket_issue_plan` (after line 2041):

```python
def add_ticket_issue_run_cost(db: Database, run_id: int, extra_cost_usd: float) -> None:
    """Fold a follow-on paid call (the implementation-plans CLI session) into
    the run row's estimated_cost_usd, alongside the planning cost (spec
    2026-07-11). Statement-level UPDATE (one column, no full-row fetch);
    coalesce covers rows that never recorded a planning cost."""
    with db.session() as s:
        s.execute(
            update(TicketIssueRun)
            .where(TicketIssueRun.id == run_id)
            .values(
                estimated_cost_usd=func.coalesce(TicketIssueRun.estimated_cost_usd, 0)
                + extra_cost_usd
            )
        )
```

Re-run the Step-1 test. Expected: PASS.

- [ ] **Step 3: Write the failing runner tests**

All edits in `worker/tests/test_ticket_issue_runner.py`.

**(a) Extend `FakeGitHub`** (dataclass at line 51). Add fields (with the other fields, before the methods):

```python
    # --- implementation-plans fakes (spec 2026-07-11) -------------------------
    repo_config_yaml: str | None = None                     # .claude-review.yml at default branch
    issue_bodies: dict[int, str] = field(default_factory=dict)   # live GitHub bodies
    updated: list[tuple[int, str]] = field(default_factory=list) # update_issue calls
    update_issue_exc_on: int | None = None                  # issue number whose PATCH raises
```

In `create_issue`, record the live body — add directly after the `self.created.append(...)` call:

```python
        self.issue_bodies[self.next_number] = body
```

Replace `get_issue` (lines 155-157) with (created issues now return their live body; the pre-feature node-backfill behavior for unknown numbers is preserved):

```python
    def get_issue(self, token, owner, repo, number):
        if number in self.issue_bodies:
            return {"title": "t", "body": self.issue_bodies[number],
                    "node_id": self.issue_nodes.get(number)}
        node = self.issue_nodes.get(number)
        return {"title": "t", "body": "b", "node_id": node} if node else None
```

Add the three new methods (after `get_issue`):

```python
    # --- implementation-plans methods -----------------------------------------

    def get_repo(self, token, owner, repo):
        return {"default_branch": "main"}

    def get_file_content(self, token, owner, repo, path, ref):
        assert path == ".claude-review.yml"
        return self.repo_config_yaml

    def update_issue(self, token, owner, repo, issue_number, body):
        if issue_number == self.update_issue_exc_on:
            raise PermanentError("GitHub 403 Resource not accessible")
        self.issue_bodies[issue_number] = body
        self.updated.append((issue_number, body))
```

**(b) Extend `FakeOdoo`** (line 160): add a `seq` field for cross-fake ordering assertions —

```python
    seq: list[str] = field(default_factory=list)
```

and in `issues_created`, as its first statement:

```python
        self.seq.append(f"issues_created:{status}")
```

**(c) Add `FakeCodeRunner`** after `FakeOdoo`:

```python
@dataclass
class FakeCodeRunner:
    """Stands in for ClaudeCodeRunner in the implementation-plans step."""
    deep_model: str = "claude-opus-4-8"
    plans: list[dict] | None = None          # -> tool_use_input {"plans": ...}
    tool_input: dict | None = None           # overrides plans (malformed-output tests)
    raise_exc: Exception | None = None
    review_calls: list[dict] = field(default_factory=list)
    ensure_calls: list[tuple] = field(default_factory=list)
    seq: list[str] = field(default_factory=list)  # shared with FakeOdoo.seq

    @contextmanager
    def repo_lock(self, owner, name):
        yield

    def ensure_repo(self, owner, name, head_sha, token):
        self.ensure_calls.append((owner, name, head_sha))
        return f"/repos/{owner}/{name}"

    def review(self, repo_path, skill, params, model=None, odoo=False, extra_dirs=None):
        self.seq.append("cli_review")
        self.review_calls.append({
            "repo_path": repo_path, "skill": skill, "params": params,
            "model": model, "odoo": odoo, "extra_dirs": extra_dirs,
        })
        if self.raise_exc:
            raise self.raise_exc
        tool_input = self.tool_input if self.tool_input is not None else {"plans": self.plans or []}
        return ClaudeResponse(
            model=model or self.deep_model, stop_reason="tool_use",
            tool_use_input=tool_input, input_tokens=5000, output_tokens=1200,
            total_cost_usd=1.25,
        )
```

Add `from contextlib import contextmanager` to the file's imports.

**(d) Wire the fixture** (`ctx_and_fakes`, lines 219-241): construct the fake runner sharing FakeOdoo's `seq`, pass it as the context's `runner`, expose it. The full fixture becomes (only the `code_runner = ...` line, the `runner=` line, and the return dict differ from today):

```python
@pytest.fixture()
def ctx_and_fakes(monkeypatch):
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)

    planner = FakePlanner(plan=_plan())
    github = FakeGitHub()
    odoo = FakeOdoo()
    code_runner = FakeCodeRunner(seq=odoo.seq)
    ctx = WorkerContext(
        db=db,
        claude=None,  # type: ignore[arg-type]
        runner=code_runner,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        reviewer=None,  # type: ignore[arg-type]
        auditor=None,  # type: ignore[arg-type]
        ticket_analyzer=None,  # type: ignore[arg-type]
        verifier=None,  # type: ignore[arg-type]
        ticket_issue_planner=planner,  # type: ignore[arg-type]
    )
    monkeypatch.setattr("worker.ticket_issue_runner.build_odoo_client", lambda ctx, _id: odoo)
    set_context(ctx)
    return {"ctx": ctx, "db": db, "planner": planner, "github": github, "odoo": odoo,
            "code_runner": code_runner}
```

**(e) One existing test needs a surgical fix.** `test_transient_callback_error_after_creation_reraises_for_rq_retry` asserts `s["github"].installation_calls == installation_calls_before` on the rerun (lines 723-726). The plans step probes installation+token+repo-config on every completed run (env default is on; the repo toggle lives in `.claude-review.yml`, which requires a token to read), so `installation_calls` is no longer a creation-short-circuit signal — `created` is. Replace lines 722-726:

```python
    # the RQ retry reruns the job: no new GitHub setup, no creates, callback re-sent
    s["odoo"].raise_exc = None
    installation_calls_before = s["github"].installation_calls
    out = run_ticket_issues(params)
    assert out["status"] == "completed"
    assert s["github"].installation_calls == installation_calls_before  # short-circuit
```

with:

```python
    # the RQ retry reruns the job: no creates, callback re-sent. (The
    # implementation-plans step probes installation + repo config on every
    # completed run, so installation_calls is no longer a short-circuit
    # signal — the created list is.)
    s["odoo"].raise_exc = None
    out = run_ticket_issues(params)
    assert out["status"] == "completed"
```

(lines 727-729, asserting `created`/callback, stay as they are.)

**(f) Append the new tests** at the end of the file:

```python
# --- implementation plans in generated issues (spec 2026-07-11) ----------------

from worker.ticket_issue_runner import (  # noqa: E402
    _strip_plan_block,
    _upsert_plan_block,
)

_PLAN_MARKER = "<!-- reva:impl-plan -->"
_PLAN_HEADER = "## 🤖 Implementation plan (AI-generated — verify before following)"


def _enable_plans(s, plans):
    s["github"].repo_config_yaml = "issue_plans: true\n"
    s["code_runner"].plans = plans


def _two_plans():
    return [
        {"number": 102, "plan_markdown": "### Approach\n- extend `models/sale.py`"},
        {"number": 103, "plan_markdown": "### Approach\n- new wizard"},
    ]


# --- marker helpers (pure) ---


def test_upsert_plan_block_appends_then_replaces():
    body = "Original text.\n\n<!-- revaticketabc -->"
    v1 = _upsert_plan_block(body, "plan v1")
    assert v1.startswith("Original text.")
    assert v1.count(_PLAN_MARKER) == 1 and _PLAN_HEADER in v1
    v2 = _upsert_plan_block(v1, "plan v2")
    assert v2.count(_PLAN_MARKER) == 1
    assert "plan v2" in v2 and "plan v1" not in v2
    assert "<!-- revaticketabc -->" in v2  # dedup marker untouched


def test_upsert_plan_block_is_backslash_safe():
    # Generated markdown may contain regex-replacement metacharacters; the
    # replacement must be literal, never a group reference.
    v1 = _upsert_plan_block("body", "plan v1")
    v2 = _upsert_plan_block(v1, r"use \g<0> and \1 literally")
    assert r"use \g<0> and \1 literally" in v2


def test_strip_plan_block_removes_only_the_block():
    body = _upsert_plan_block("Keep me.\n\n<!-- revaticketabc -->", "plan v1")
    stripped = _strip_plan_block(body)
    assert "plan v1" not in stripped and _PLAN_MARKER not in stripped
    assert "Keep me." in stripped and "revaticketabc" in stripped


# --- toggle matrix (spec testing section) ---


def test_issue_plans_off_by_default_no_cli_session(ctx_and_fakes):
    # repo off (default): no CLI call, no edits, and NO ops event — a switch
    # doing its job is not a degradation.
    s = ctx_and_fakes
    out = run_ticket_issues(_make_params(s["db"]))
    assert out["status"] == "completed"
    assert s["code_runner"].review_calls == []
    assert s["github"].updated == []
    assert [c for c, _e, _d in _ops_events(s["db"]) if c == "issue_plans"] == []


def test_issue_plans_env_kill_switch_beats_repo_opt_in(ctx_and_fakes, monkeypatch):
    s = ctx_and_fakes
    _enable_plans(s, _two_plans())
    monkeypatch.setenv("REVA_ISSUE_PLANS_ENABLED", "false")
    out = run_ticket_issues(_make_params(s["db"]))
    assert out["status"] == "completed"
    assert s["code_runner"].review_calls == []
    assert [c for c, _e, _d in _ops_events(s["db"]) if c == "issue_plans"] == []


def test_issue_plans_happy_path_posts_marked_blocks(ctx_and_fakes):
    s = ctx_and_fakes
    s["github"].repo_config_yaml = "issue_plans: true\nodoo: true\n"
    s["code_runner"].plans = _two_plans()
    params = _make_params(s["db"])

    out = run_ticket_issues(params)

    assert out["status"] == "completed"
    # ONE CLI session for the whole run (locked decision), deep model,
    # default-branch clone (head_sha=None -> origin/HEAD).
    assert len(s["code_runner"].review_calls) == 1
    call = s["code_runner"].review_calls[0]
    assert call["skill"] == "reva-issue-plans"
    assert call["model"] == "claude-opus-4-8"
    assert call["odoo"] is True
    assert s["code_runner"].ensure_calls == [("acme", "widgets", None)]
    # both created children are in the CLI input, with ticket context
    assert '"number": 102' in call["params"]["issues"]
    assert '"number": 103' in call["params"]["issues"]
    assert call["params"]["ticket_name"] == "Login page broken"

    # marked block appended to each child body; the parent epic (101) untouched
    edited = dict(s["github"].updated)
    assert set(edited) == {102, 103}
    body = s["github"].issue_bodies[102]
    assert _PLAN_MARKER in body and "<!-- /reva:impl-plan -->" in body
    assert _PLAN_HEADER in body
    assert "- extend `models/sale.py`" in body
    # the original body (AC + Odoo back-link + dedup marker) is preserved
    assert "- [ ] criterion 1" in body
    assert params["ticket_url"] in body
    assert "<!-- revaticket" in body


def test_issue_plans_run_strictly_after_created_callback(ctx_and_fakes):
    # Locked ordering: plans must never delay or endanger the contract flow.
    s = ctx_and_fakes
    _enable_plans(s, _two_plans())
    run_ticket_issues(_make_params(s["db"]))
    seq = s["odoo"].seq
    assert "issues_created:created" in seq and "cli_review" in seq
    assert seq.index("issues_created:created") < seq.index("cli_review")


# --- fail-soft (spec testing section) ---


def test_issue_plans_cli_failure_is_fail_soft(ctx_and_fakes):
    s = ctx_and_fakes
    _enable_plans(s, None)
    s["code_runner"].raise_exc = PermanentError("claude exited 1: boom")
    params = _make_params(s["db"])

    out = run_ticket_issues(params)

    assert out["status"] == "completed"
    assert writers.get_ticket_issue_run(s["db"], params["run_id"])["status"] == "completed"
    assert s["odoo"].calls[0]["status"] == "created"      # callback already sent
    events = _ops_events(s["db"])
    assert ("issue_plans", "step_failed") in [(c, e) for c, e, _ in events]


def test_issue_plans_malformed_output_is_fail_soft(ctx_and_fakes):
    s = ctx_and_fakes
    _enable_plans(s, None)
    s["code_runner"].tool_input = {"summary": "not a plans payload"}
    out = run_ticket_issues(_make_params(s["db"]))
    assert out["status"] == "completed"
    assert s["github"].updated == []
    events = _ops_events(s["db"])
    assert ("issue_plans", "step_failed") in [(c, e) for c, e, _ in events]


def test_issue_plans_edit_failure_is_isolated_per_issue(ctx_and_fakes):
    # One rejected PATCH must not sink the other issues' plans (the board
    # step's _set_field posture).
    s = ctx_and_fakes
    _enable_plans(s, _two_plans())
    s["github"].update_issue_exc_on = 102
    out = run_ticket_issues(_make_params(s["db"]))
    assert out["status"] == "completed"
    assert [n for n, _b in s["github"].updated] == [103]
    events = _ops_events(s["db"])
    assert ("issue_plans", "issue_update_failed") in [(c, e) for c, e, _ in events]


def test_issue_plans_budget_skip_no_cli_call(ctx_and_fakes):
    import dataclasses
    s = ctx_and_fakes
    _enable_plans(s, _two_plans())
    writers.record_claude_spend(s["db"], "review", 50.0)      # trailing spend
    set_context(dataclasses.replace(s["ctx"], daily_budget_usd=10.0))
    out = run_ticket_issues(_make_params(s["db"]))
    assert out["status"] == "completed"
    assert s["code_runner"].review_calls == []                # no paid call
    assert s["github"].updated == []
    events = _ops_events(s["db"])
    assert ("issue_plans", "skipped_budget") in [(c, e) for c, e, _ in events]


# --- idempotency / robustness (spec testing section) ---


def test_issue_plans_rerun_replaces_block_and_strips_prior_input(ctx_and_fakes):
    s = ctx_and_fakes
    _enable_plans(s, [
        {"number": 102, "plan_markdown": "### Approach\nfirst version"},
        {"number": 103, "plan_markdown": "plan for 103"},
    ])
    params = _make_params(s["db"])
    run_ticket_issues(params)

    s["code_runner"].plans = [
        {"number": 102, "plan_markdown": "### Approach\nsecond version"},
        {"number": 103, "plan_markdown": "plan for 103 v2"},
    ]
    run_ticket_issues(params)   # requeue/resend: creation short-circuits

    body = s["github"].issue_bodies[102]
    assert body.count(_PLAN_MARKER) == 1                 # replaced, not stacked
    assert "second version" in body and "first version" not in body
    # the resumed run's CLI input carries the CLEAN body, not the prior plan
    second_input = s["code_runner"].review_calls[1]["params"]["issues"]
    assert "first version" not in second_input


def test_issue_plans_hallucinated_and_omitted_numbers_skipped(ctx_and_fakes):
    s = ctx_and_fakes
    _enable_plans(s, [
        {"number": 999, "plan_markdown": "for an issue we never created"},
        {"number": 102, "plan_markdown": "real plan"},
        # 103 omitted by the model
    ])
    out = run_ticket_issues(_make_params(s["db"]))
    assert out["status"] == "completed"
    assert [n for n, _b in s["github"].updated] == [102]   # 999 never edited, 103 skipped
    assert "real plan" in s["github"].issue_bodies[102]
    assert _PLAN_MARKER not in s["github"].issue_bodies[103]


def test_issue_plans_redacts_internal_paths(ctx_and_fakes):
    # SECU-21: the plan is posted to GitHub — internal server paths must not leak.
    s = ctx_and_fakes
    _enable_plans(s, [
        {"number": 102, "plan_markdown": "Start in /repos/acme/widgets/models/foo.py here."},
        {"number": 103, "plan_markdown": "x"},
    ])
    run_ticket_issues(_make_params(s["db"]))
    body = s["github"].issue_bodies[102]
    assert "/repos/acme/widgets" not in body
    assert "<path>" in body


def test_issue_plans_cost_on_ledger_and_run_row(ctx_and_fakes):
    s = ctx_and_fakes
    _enable_plans(s, _two_plans())
    params = _make_params(s["db"])
    run_ticket_issues(params)

    from reva.db.models import ClaudeSpend
    with s["db"].session() as sess:
        kinds = [r.kind for r in sess.query(ClaudeSpend).all()]
    assert "issue_plans" in kinds
    # run row: planning cost (Messages-API estimate, > 0) + the CLI's 1.25
    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["estimated_cost_usd"] > 1.25
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_runner.py -k "issue_plans or plan_block" -v`
Expected: RED — collection itself fails with `ImportError: cannot import name '_strip_plan_block' from 'worker.ticket_issue_runner'` (the helpers don't exist yet). That is this step's failing state; Step 5 turns it green. Also re-run the untouched suite portion once (`.venv/bin/python -m pytest tests/test_ticket_issue_runner.py -k "not issue_plans and not plan_block" -q`) — it fails the same way until Step 5 because the module-level import at the end of the test file blocks collection; do not "fix" that by moving tests to a new file, the helpers land next.

- [ ] **Step 5: Implement the step**

All edits in `worker/worker/ticket_issue_runner.py`.

**(a) Imports** — the block at lines 26-41 becomes (additions marked; keep the existing lines and ordering conventions, ruff-clean):

```python
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

import structlog
import yaml
from pydantic import ValidationError
from rq import get_current_job

from reva.config import issue_plans_enabled
from reva.cost import estimate_cost
from reva.db import writers
from reva.errors import PermanentError, TransientError
from reva.github_urls import parse_github_project_url, parse_github_repo_url
from reva.review_formatter import _redact_internal_paths
from reva.types import RepoConfig, TicketIssueJobParams
from worker.change_note_delivery import maybe_deliver_change_notes
from worker.runner import budget_exceeded, build_odoo_client, get_context, instance_budget_exceeded
```

**(b) Markers + helpers + step** — insert after `_project_step` (after line 678), before `_plan_and_create`:

```python
# --- Implementation plans in generated issues (spec 2026-07-11) ---------------

# Markers fencing REVA's plan block inside an issue body. Distinct from the
# sibling /describe feature's `reva:describe` markers. Frozen strings: changing
# them orphans every existing plan block (replacement would stop matching).
_PLAN_MARKER_START = "<!-- reva:impl-plan -->"
_PLAN_MARKER_END = "<!-- /reva:impl-plan -->"
_PLAN_HEADER = "## 🤖 Implementation plan (AI-generated — verify before following)"
# Well-formed block only; DOTALL spans the markdown between the markers.
_PLAN_BLOCK_RE = re.compile(
    re.escape(_PLAN_MARKER_START) + r".*?" + re.escape(_PLAN_MARKER_END), re.DOTALL
)


def _strip_plan_block(body: str) -> str:
    """Issue body without REVA's plan block — the CLI input on a resumed run
    must not contain the previous plan (it would anchor the rewrite)."""
    return _PLAN_BLOCK_RE.sub("", body).rstrip()


def _upsert_plan_block(body: str, plan_markdown: str) -> str:
    """Replace the existing marked block in place, else append one. Function
    replacement (not a template string) so backslashes / \\g<…> in the
    generated markdown stay literal, never regex group references."""
    block = (
        f"{_PLAN_MARKER_START}\n{_PLAN_HEADER}\n\n"
        f"{plan_markdown.strip()}\n{_PLAN_MARKER_END}"
    )
    if _PLAN_BLOCK_RE.search(body):
        return _PLAN_BLOCK_RE.sub(lambda _m: block, body, count=1)
    return f"{body.rstrip()}\n\n{block}" if body.strip() else block


def _load_repo_config(ctx, token: str, owner: str, repo: str) -> RepoConfig:
    """.claude-review.yml at the default-branch head. Missing/malformed →
    defaults (issue_plans stays off). Mirrors Reviewer._load_repo_config;
    module-level because this runner has no reviewer instance."""
    default_branch = ctx.github.get_repo(token, owner, repo).get("default_branch") or "HEAD"
    raw = ctx.github.get_file_content(token, owner, repo, ".claude-review.yml", default_branch)
    if not raw:
        return RepoConfig()
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        logger.warning("issue_plans_repo_config_parse_failed", repo=f"{owner}/{repo}")
        return RepoConfig()
    if not isinstance(parsed, dict):
        return RepoConfig()
    try:
        return RepoConfig.model_validate(parsed)
    except ValidationError:
        logger.warning("issue_plans_repo_config_invalid", repo=f"{owner}/{repo}")
        return RepoConfig()


def _issue_plans_step(ctx, params: TicketIssueJobParams, issues: list[dict], log) -> None:
    """Research-grounded implementation plan appended to each created issue
    (spec 2026-07-11). Fail-soft like _project_step: plans are a bonus — any
    failure logs + ops-events (component="issue_plans") and the run stays
    completed. Runs strictly AFTER the issues-created callback (never delays
    or endangers the contract flow); a requeued/resumed run replaces existing
    blocks idempotently. Toggle-skips are debug-logged with NO ops event — a
    switch doing its job is not a degradation.
    """
    try:
        if not issue_plans_enabled():
            log.debug("issue_plans_disabled_by_env")
            return
        numbered = [i for i in issues if i.get("number") is not None]
        if not numbered:
            return
        parsed = parse_github_repo_url(params.github_url)
        if parsed is None:  # _plan_and_create already raised on this; defensive
            return
        owner, repo = parsed
        installation_id = ctx.github.get_repo_installation_id(owner, repo)
        token = ctx.github.get_installation_token(installation_id)
        repo_config = _load_repo_config(ctx, token, owner, repo)
        if not repo_config.issue_plans:
            log.debug("issue_plans_disabled_by_repo_config")
            return

        # Budget gate: over budget skips WITH an ops event (spec step 1) —
        # unlike a toggle, this is a degradation the operator must see.
        spent = budget_exceeded(ctx)
        if spent is not None:
            log.warning("issue_plans_over_budget", spent_usd=round(spent, 2))
            writers.record_ops_event(
                ctx.db, "issue_plans", "warning", "skipped_budget",
                {"run_id": params.run_id, "ticket_id": params.ticket_id,
                 "spent_usd": round(spent, 2)},
            )
            return

        # Current GitHub bodies: the CLI input AND the edit base (creation
        # dropped the body from the persisted items — it lives on GitHub).
        # The previous plan block is stripped from the input on resumed runs.
        bodies: dict[int, str] = {}
        for item in numbered:
            fetched = ctx.github.get_issue(token, owner, repo, item["number"])
            if fetched is not None:  # deleted on GitHub → no plan for it
                bodies[item["number"]] = fetched["body"]
        if not bodies:
            return
        issues_payload = json.dumps(
            [
                {"number": i["number"], "title": i.get("title", ""),
                 "body": _strip_plan_block(bodies[i["number"]])}
                for i in numbered if i["number"] in bodies
            ],
            indent=2, ensure_ascii=False,
        )
        row = writers.get_ticket_issue_run(ctx.db, params.run_id) or {}
        skill_params = {
            "repo": f"{owner}/{repo}",
            "ticket_name": params.name,
            "ticket_summary": row.get("plan_summary") or params.description,
            "issues": issues_payload,
        }
        extra_dirs: list[str] | None = None
        if ctx.core_knowledge is not None and repo_config.odoo_version:
            core_version = ctx.core_knowledge.resolve(repo_config.odoo_version)
            if core_version is None:
                # Config drift: degrade + record, per the visibility invariant.
                log.warning("issue_plans_core_knowledge_unavailable",
                            version=repo_config.odoo_version)
                writers.record_ops_event(
                    ctx.db, "core_knowledge", "warning", "version_unavailable",
                    {"repo": f"{owner}/{repo}", "requested": repo_config.odoo_version},
                )
            else:
                extra_dirs = ctx.core_knowledge.core_paths(core_version)
                skill_params["core_knowledge"] = (
                    f"Odoo {core_version} core knowledge is available read-only under "
                    f"{', '.join(extra_dirs)}. Use it to check whether a requirement "
                    "is already covered by standard Odoo before planning custom code. "
                    f"Catalog: {ctx.core_knowledge.catalog_path(core_version)}."
                )

        # ONE CLI session for the whole run (locked decision): amortizes the
        # clone/context and lets plans cross-reference each other. Deep model,
        # like audits — planning is repo-wide reasoning, not a cost-sensitive
        # diff path. head_sha=None ⇒ origin/HEAD (the default-branch head).
        with ctx.runner.repo_lock(owner, repo):
            repo_path = ctx.runner.ensure_repo(owner, repo, None, token)
            response = ctx.runner.review(
                repo_path=repo_path,
                skill="reva-issue-plans",
                params=skill_params,
                model=ctx.runner.deep_model,
                odoo=repo_config.odoo,
                extra_dirs=extra_dirs,
            )

        # The CLI already charged us: record spend BEFORE parsing so a
        # malformed output can't hide the cost from the budget ledger (M1).
        cost = response.total_cost_usd or estimate_cost(
            response.model or "", response.input_tokens, response.output_tokens,
            response.cache_read_tokens, response.cache_creation_tokens,
        )
        writers.record_claude_spend(ctx.db, "issue_plans", cost)
        writers.add_ticket_issue_run_cost(ctx.db, params.run_id, cost)

        tool_input = response.tool_use_input
        plans = tool_input.get("plans") if isinstance(tool_input, dict) else None
        if not isinstance(plans, list):
            raise PermanentError("issue plans: Claude returned no plans list")
        by_number: dict[int, str] = {}
        for entry in plans:
            number = entry.get("number") if isinstance(entry, dict) else None
            if not isinstance(number, int) or not isinstance(entry.get("plan_markdown"), str):
                log.warning("issue_plan_entry_malformed", entry=str(entry)[:100])
                continue
            by_number[number] = entry["plan_markdown"]

        wanted = set(bodies)
        for number in sorted(set(by_number) - wanted):
            # Hallucinated / out-of-run number: never edit an issue this run
            # didn't create (spec: skip with a warning log).
            log.warning("issue_plan_number_unknown", issue=number)
        posted = 0
        for number in sorted(wanted):
            plan_md = by_number.get(number)
            if plan_md is None:
                log.warning("issue_plan_missing", issue=number)
                continue
            # SECU-21: internal paths are redacted from anything posted to GitHub.
            body = _upsert_plan_block(bodies[number], _redact_internal_paths(plan_md))
            try:
                ctx.github.update_issue(token, owner, repo, number, body=body)
                posted += 1
                log.info("issue_plan_posted", issue=number)
            except Exception as exc:
                # Per-issue fail-soft (the board step's _set_field posture): one
                # rejected PATCH must not sink the remaining issues' plans.
                log.warning("issue_plan_update_failed", issue=number, exc_info=True)
                writers.record_ops_event(
                    ctx.db, "issue_plans", "warning", "issue_update_failed",
                    {"run_id": params.run_id, "issue": number, "error": str(exc)[:200]},
                )
        log.info("issue_plans_done", posted=posted, planned=len(by_number))
    except Exception as exc:
        log.warning("issue_plans_step_failed", exc_info=True)
        writers.record_ops_event(
            ctx.db, "issue_plans", "warning", "step_failed",
            {"run_id": params.run_id, "ticket_id": params.ticket_id,
             "error": str(exc)[:300]},
        )
```

**(c) Wire it into `run_ticket_issues`** — after the `odoo.issues_created` try/except (its `except PermanentError:` handler ends at line 436) and before `log.info("ticket_issues_done", issues=len(union))` (line 438), insert:

```python
    # Implementation plans (fail-soft; spec 2026-07-11) — strictly AFTER the
    # issues-created callback: plans must never delay or endanger the contract
    # flow. Runs on the run's OWN issues (not the union, not the parent epic);
    # a requeued/resumed run replaces existing plan blocks idempotently. On a
    # transient callback error the re-raise above means plans only run on the
    # retry that successfully (re-)sent the callback.
    _issue_plans_step(ctx, params, issues, log)
```

- [ ] **Step 6: Run the covering suites**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_runner.py tests/test_ticket_issue_writers.py tests/test_ticket_links.py -q`
Expected: all PASS — the 15 new tests AND every pre-existing test (default-off means existing tests reach the step, probe the fake's `repo_config_yaml=None` → `RepoConfig()` → debug-skip; only the one assertion fixed in Step 3(e) changed).

Run the neighboring suites the shared fakes/context touch: `cd worker && .venv/bin/python -m pytest tests/test_runner.py tests/test_reviewer.py tests/test_auditor.py -q`
Expected: all PASS (no shared fixtures with those files, but `worker.runner` imports changed nothing — this is a cheap regression sweep).

- [ ] **Step 7: Commit**

```bash
git add reva/db/writers.py worker/worker/ticket_issue_runner.py worker/tests/test_ticket_issue_runner.py worker/tests/test_ticket_issue_writers.py
git commit -m 'feat(tickets): implementation plans appended to generated issues (deep-model CLI, fail-soft)'
```

---

### Task 5: Verification sweep + spec sync

**Files:**
- Modify: `docs/superpowers/specs/2026-07-11-issue-implementation-plans-design.md` (Status line at line 4; flat-skill-file correction at lines 42-44)

- [ ] **Step 1: Full gates** (shared `reva/` touched → all three services)

Run from the repo root:

```bash
make test
worker/.venv/bin/ruff check reva worker/worker api/app scheduler/scheduler
git status --porcelain tui/ contracts/
```

Expected: worker, api, and scheduler suites all green; ruff clean (no output, exit 0); the last command prints nothing (no TUI or contracts changes — Global Constraints). Advisory (non-blocking): `mypy reva worker/worker api/app scheduler/scheduler --ignore-missing-imports` shows no NEW errors vs. main.

- [ ] **Step 2: Spec sync**

In `docs/superpowers/specs/2026-07-11-issue-implementation-plans-design.md`:

Replace line 4:

```markdown
- **Status:** approved (design), not yet planned
```

with:

```markdown
- **Status:** approved — implemented; see `plans/2026-07-11-issue-implementation-plans.md`.
```

And correct the skill path (code wins over prose — skills are flat `.md` files, not directories). Replace (lines 42-44):

```markdown
3. **One headless CLI session for the whole run** (`REVA_DEEP_MODEL`, new skill
   `prompts/skills/reva-issue-plans/`): input = the run's created issues
```

with:

```markdown
3. **One headless CLI session for the whole run** (`REVA_DEEP_MODEL`, new skill
   `prompts/skills/reva-issue-plans.md` — skills are flat `.md` files, not
   directories; the spec originally said `reva-issue-plans/`): input = the
   run's created issues
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-07-11-issue-implementation-plans-design.md
git commit -m 'docs(specs): issue implementation plans -- mark implemented, correct flat-skill-file path'
```

**Honest-status note for the final report:** all coverage is unit-level — SQLite + fake GitHub/Odoo/CLI-runner; no live headless-CLI session, no live GitHub PATCH, no real clone. The spec's staging gate remains open: one live opted-in create-issues run (repo with `issue_plans: true`), read the generated plans for grounding quality before enabling more repos. The plans step adds ~4 GitHub metadata calls (installation, token, repo, config file) to every completed create-issues run even for opted-out repos — accepted (runs are rare and minutes-long; the toggle lives in `.claude-review.yml`, which needs a token to read). Cost lands in the existing `estimated_cost_usd` column — no migration was needed.
