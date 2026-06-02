# prompts/ — REVA's prompt content

**What's in these files shapes every review.** Treat changes here like product
changes, not implementation details. There are two delivery paths, matching
REVA's two Claude clients:

- **`skills/`** → the headless **Claude Code CLI** (`ClaudeCodeRunner`). Used
  for all PR reviews and repo audits. See [`skills/README.md`](skills/README.md).
- **The Markdown templates here** → the **Messages API** (`PromptBuilder` +
  `ClaudeClient`). Used for ticket analysis and inline-comment replies.

## Files (Messages-API path)

| File | Role | Cached on Claude side? |
|---|---|---|
| `system.md` | REVA identity, anti-injection guard, tool_use contract, severity/category definitions, global rules | Yes (block 1) |
| `odoo19.md` | Odoo-specific review rules | Yes (block 2, conditional) |
| `ticket_analysis.md` | Ticket-analysis instructions | n/a |
| `diff_review.md`, `deep_review.md` | Legacy user-message templates from the original Messages-API review path; retained for reference (reviews now run via `skills/`). | No |
| `CHANGELOG.md` | Version history; the first heading is parsed as `prompt_version` and stored on every `review_runs` row. | n/a |

## Cache strategy (Messages-API path)

`PromptBuilder.build_system_blocks` tags each cacheable block with
`cache_control: ephemeral`, in order: `system.md` → `odoo19.md` (conditional) →
the repo's `CLAUDE.md` (if present) → `custom_instructions` from
`.claude-review.yml`. Repeated calls hit the cache and cut input cost ~90%.

> On the CLI path, a reviewed repo's own `CLAUDE.md` (and `.claude/`, `.mcp.json`,
> `AGENTS.md`) is **deleted from the clone before the CLI runs** (SECU-1): the
> clone is attacker-controlled, and the CLI would otherwise auto-load it as an RCE
> vector. Per-repo customisation goes through `.claude-review.yml`, which REVA
> fetches via the GitHub API and validates — not through repo-supplied CLI config.

## Versioning & tuning

Bump the version on every prompt change (add a heading at the top of
`CHANGELOG.md`). `PromptBuilder.get_version()` parses it and persists it on
`review_runs.prompt_version`, so finding rates stay A/B-comparable across
versions. Don't edit prompts without bumping — you lose comparability. Watch the
`review_feedback` table to spot over-eager finding categories, then adjust the
relevant section.

## Output contract

REVA uses **tool_use** structured output (`submit_review`). The schema is
generated from `reva/types.py` (`Finding` / `ReviewResult`) via
`reva/review_tool.py` — never hand-write a second schema. If you change those
models, update the prose in `system.md` (and the skills) to describe the new
fields.
