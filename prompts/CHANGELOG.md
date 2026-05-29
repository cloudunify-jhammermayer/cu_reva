## v1.1 — Shared review guidance on the CLI path

- Added `review_guidance.md`: path-agnostic governance (identity, anti-injection
  guard, severity/category/confidence, conduct rules). The headless-CLI runner
  now prepends it + `odoo19.md` to every review skill, so the Odoo ruleset and
  the injection guard finally apply to PR reviews and audits.
- Slimmed the four `skills/*.md` to task + output contract; the duplicated
  severity/category/rules blocks now live once in `review_guidance.md`.
- `reva-repo-audit.md`: switched from `Bash` (not an allowed tool) to `Grep`/`Glob`;
  recognizes both `custom_addons/` and `custom-addons/`. Runner allows `Glob`.
- `system.md`: fixed the `1#` heading typo; removed the contradictory
  "reject PRs with >5 critical bugs" rule (REVA has no reject event).

## v1.0 — Initial release

Initial REVA prompt set:

- `system.md` — REVA identity, personality, anti-injection guard, tool_use
  output contract, severity/category definitions, confidence scoring,
  global rules.
- `diff_review.md` — user-message template for default reviews.
- `deep_review.md` — user-message template for `/deep-review` triggered
  reviews, with extra emphasis on architectural impact, cross-file
  regressions, migration safety, backwards compatibility, and end-to-end
  security analysis.
- `odoo19.md` — Odoo 19-specific review rules, conditionally included when
  the repository's `.claude-review.yml` sets `framework: odoo`.

`PromptBuilder.get_version()` reads the first heading of this file. Each
entry must start `## v<X.Y>` so the parser picks it up.
