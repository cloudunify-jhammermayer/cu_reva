# prompts/ — REVA's prompt content

The files in this directory are loaded at runtime by `PromptBuilder` and
sent to the Claude Messages API. **What's in these files shapes every
review.** Treat changes here like product changes, not implementation details.

## Files

| File | Role | Cached on Claude side? |
|---|---|---|
| `system.md` | REVA identity, anti-injection guard, tool_use contract, severity/category definitions, global rules | Yes (block 1, every review) |
| `odoo19.md` | Odoo 19-specific review rules | Yes (block 2, only when repo config sets `framework: odoo`) |
| `diff_review.md` | User-message template for default reviews | No (changes every PR) |
| `deep_review.md` | User-message template for `/deep-review` triggered reviews | No |
| `CHANGELOG.md` | Version history; first heading is parsed as `prompt_version` and stored on every `review_runs` row | n/a — read at runtime |

## Cache strategy

The Claude Messages API caches at content-block boundaries when each block
carries `cache_control: {"type": "ephemeral"}`. `PromptBuilder.build_system_blocks`
attaches that marker to each cacheable block, in this order:

1. `system.md`
2. `odoo19.md` (conditional on `framework: odoo`)
3. `CLAUDE.md` from the repo (if present)
4. `custom_instructions` from the repo's `.claude-review.yml` (if present)

On repeated reviews of the same repo + prompt version, cache hits cut input
cost roughly 90%.

## Versioning

Every prompt change bumps the version. Add a new heading at the top of
`CHANGELOG.md`:

```markdown
## v1.1 — Reduced false positives

- Increased confidence-threshold guidance for style findings.
```

`PromptBuilder.get_version()` parses the version string from the first line.
That string is persisted on `review_runs.prompt_version` for every review,
so you can A/B compare finding rates across versions later via SQL.

## Tuning loop

After deploying a new version:

1. Watch the `review_feedback` table — low approval rate on a category often signals over-eager findings of that type.
2. Adjust the relevant section of `system.md` or the Odoo file.
3. Bump the changelog. Don't edit the prompt without bumping; you lose A/B comparability.

## Output contract reminder

REVA uses **tool_use** for structured output. `system.md` tells Claude to
call `submit_review` exactly once and emit no prose. The worker forces this
via `tool_choice` and discards any free-form text. If you change the schema
in `worker/worker/types.py` (`Finding` / `ReviewResult`), the tool schema
regenerates automatically — but you may need to update `system.md` to
describe the new fields for Claude.
