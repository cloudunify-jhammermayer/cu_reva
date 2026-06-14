# prompts/skills/ — headless Claude Code review skills

These Markdown files are the instructions for the **headless Claude Code CLI**
(`reva/claude_code_runner.py`). For each review, the runner reads the skill
file, appends the task parameters (XML-delimited so user content can't be
confused with instructions) and an `output_path`, and runs `claude --print`
inside the cloned repo. The skill tells Claude to explore the repo with
`Read`/`Grep` and then **write the `submit_review` JSON to `output_path`** with
the `Write` tool. REVA reads that file back and validates it against the
`Finding` / `ReviewResult` schema.

| Skill | Used for | What Claude sees / does |
|---|---|---|
| `reva-diff-review.md` | default push / `/review` (diff mode) / `/review-all` (diff, all paths) | The diff + the cloned repo; reads connected files before deciding. |
| `reva-full-review.md` | `/deep-review` / full mode | Explores the repo freely, not just the diff. |
| `reva-delta-review.md` | incremental review when a prior completed review exists | Reviews only the *compare* diff since the last reviewed SHA. |
| `reva-migration-review.md` | any PR whose diff touches `migrations/<ver>/{pre,post,end}-migrate.py` | Odoo upgrade-script review (destructive DDL, idempotency, JSONB, SQL injection). |
| `reva-xml-review.md` | an **XML-only** PR diff | Odoo view/QWeb review; resolves xpath/`inherit_id`/`ref` against the clone. |
| `reva-repo-audit.md` | on-demand repo audit (API / TUI) | Audits the whole default branch, produces a structured report. |

**Skill selection** is centralized in `Reviewer._select_skill` (`worker/worker/reviewer.py`),
evaluated on the final post-filter diff (after the trivial-diff short-circuit) so content-driven
routing sees exactly what Claude will. Precedence, highest first:
**migration > delta > xml-only > diff/full**. Migration scripts override the mode/delta choice
(highest blast radius) while keeping `delta_base_sha` for the resolution pass; XML-only routing does
not apply to delta reviews (v1).

Optional structured params some skills receive (added only when relevant, so the cached prefix stays
stable): `test_coverage`, `already_reported`, `stated_intent` (closed GitHub issues), `manifest_audit`
(deterministic `__manifest__.py` checks).

## Why skills instead of a giant prompt

The skill format lets Claude Code do agentic exploration (open files, grep,
follow imports) instead of reviewing a diff in isolation — the whole reason for
the headless-CLI path. Keeping each mode in its own file means the diff/full/
delta/audit behaviours evolve independently.

## Contract (don't break these)

- The output **must** be valid JSON at `output_path` matching the tool schema
  (`reva/types.py`). A missing/invalid file is a `PermanentError`.
- `Finding.title` ≤ 80 chars; `confidence` guidance is in each skill.
- Severity / category vocab must match `system.md` and the schema.
- Skills are installed into the worker image (see the headless-Claude design
  spec) and version-bumped alongside `prompts/CHANGELOG.md`.
