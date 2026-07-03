# prompts/ — REVA's prompt content

**What's in these files shapes every review.** Treat changes here like product
changes, not implementation details. There are two delivery paths, matching
REVA's two Claude clients:

- **`skills/` + `review_guidance.md` + `odoo19.md`** → the headless **Claude
  Code CLI** (`ClaudeCodeRunner`). Used for all PR reviews and repo audits. The
  CLI assembles the prompt itself (`review_guidance.md` preamble → `odoo19.md`
  for Odoo repos → the selected `skills/*.md`). See
  [`skills/README.md`](skills/README.md).
- **The remaining Markdown templates** → the **Messages API** (`ClaudeClient`).
  Used for ticket analysis, ticket-issue planning, inline-comment replies, and
  the learned-memory distiller — each reads its own template directly.

## Files

| File | Role | Path |
|---|---|---|
| `review_guidance.md` | Always-on review governance: identity, severity/category/confidence definitions, security & conduct rules, shared skill-parameter handling | CLI |
| `odoo19.md` | Odoo-specific review rules (prepended for Odoo repos) | CLI |
| `skills/*.md` | Per-mode task sections (diff/delta/full/xml/migration/audit) | CLI |
| `ticket_analysis.md`, `ticket_issues.md`, `review_memory.md` | Messages-API task prompts | Messages API |
| `CHANGELOG.md` | Version history; the first heading is parsed as `prompt_version` and stored on every `review_runs` row. | n/a |

> On the CLI path, a reviewed repo's own `CLAUDE.md` (and `.claude/`, `.mcp.json`,
> `AGENTS.md`) is **deleted from the clone before the CLI runs** (SECU-1): the
> clone is attacker-controlled, and the CLI would otherwise auto-load it as an RCE
> vector. Per-repo customisation goes through `.claude-review.yml`, which REVA
> fetches via the GitHub API and validates — not through repo-supplied CLI config.

## Versioning & tuning

Bump the version on every prompt change (add a heading at the top of
`CHANGELOG.md`). `PromptBuilder.get_version()` parses it and persists it on
`review_runs.prompt_version`, so finding rates stay A/B-comparable across
versions. Don't edit prompts without bumping — you lose comparability, and the
boot-time drift guard (`compute_prompt_hashes`) alerts. Watch the
`review_feedback` table to spot over-eager finding categories, then adjust the
relevant section.

## Output contract

On the CLI path each skill instructs Claude to Write a JSON file matching the
shape in that skill's "Output format" section; the worker parses it into
`Finding` / `ReviewResult` (`reva/types.py`) and recomputes `risk_level` and the
finding cap itself. If you change those models, update every skill's output
example to match.
