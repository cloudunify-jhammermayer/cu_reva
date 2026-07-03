## v1.8 — Issue types + tldr titles for ticket-issue planning

- `ticket_issues.md`: each planned issue now carries a `type` code
  (`BUG`/`FEAT`/`CR`/`CONF`/`DEV`/`MIG`/`SUP`/`DOC`) and `title` is a
  ≤30-character tldr — the worker renders the full GitHub title
  (`[TYPE] <ticket_id> - <tldr> (n/total)`) and applies the type as a
  label. Typed requests (Odoo wizard) fix the type for every issue.

## v1.7 — Re-baseline Tier 2 prompt set

- No new prompt behaviour. The Tier 2 additions promised under v1.6
  ("Further Tier 2 skill/prompt additions land under this version.") —
  `reva-migration-review.md`, `reva-xml-review.md`, the `__manifest__.py`
  checks, intent grounding, and the security-model consistency cross-check —
  were committed *after* v1.6's content-hash baseline was first recorded, so
  the worker flagged `prompt_drift_detected` on every boot. Bumping the
  version snapshots the current files as a fresh immutable baseline and
  clears the warning; the prompt content itself is unchanged from what v1.6
  already describes below.

## v1.6 — Tier 2 review-intelligence prompts

- `reva-delta-review.md`: added an "Already-reported findings" block. Delta
  re-reviews now receive an `already_reported` param (the prior review's still-open
  findings) and must NOT re-emit them as new inline comments — fixes duplicate
  comments on follow-up pushes. (Further Tier 2 skill/prompt additions land under
  this version.)
- `review_guidance.md`: added a "Stated intent" section. When a PR body closes a
  GitHub issue (`closes #N`), REVA now passes a nonce-fenced `stated_intent` param;
  the model checks the diff against it (contradiction → bug, unimplemented/scope
  creep → maintainability) and scopes the check to new changes on delta reviews.
- All four review skills + `odoo19.md`: added `__manifest__.py` checks. The
  diff/delta/full skills receive a deterministic `manifest_audit` param (missing
  data files, security-before-views order, version format) to surface; full/audit
  additionally do the used-but-undeclared `depends` cross-check.
- New `reva-migration-review.md` skill. PRs touching Odoo upgrade scripts
  (`migrations/<ver>/{pre,post,end}-migrate.py`) are path-routed to it (overrides
  the mode/delta skill); it checks destructive DDL, non-idempotent backfills,
  ORM-vs-SQL staging, JSONB translations, and SQL injection.
- New `reva-xml-review.md` skill + `.xml` is no longer blanket-stripped from the
  diff (third-party odoo/enterprise XML still dropped by prefix). XML-only PRs route
  to it: resolves xpath/inherit_id/ref targets against the clone, applies the view
  rules (t-esc→t-out, inline `<script>`/CDN CSP, explicit inherit_id, `<card>`,
  noupdate). Per-repo `max_xml_diff_lines`/`max_xml_diff_tokens` cap view dumps.
- All four review skills: added a "Security-model consistency" cross-check — when a
  diff adds a model (`_name =` / new-table `_inherit`), check `ir.model.access.csv`
  for a missing ACL (major) and `ir.rule` for a company-scoped model (major). Full/
  delta/audit get the full procedure; diff gets a bounded variant. Backed by a new
  deterministic `missing_record_rule` severity floor.

## v1.5 — Consultant DOCX as planning basis

- `ticket_issues.md`: when Odoo forwards a consultant specification document
  (Contract 1 `description_docx`, project tasks only), it is the authoritative
  planning basis — the worker extracts its text and the prompt plans from it
  instead of the ticket description/analysis.

## v1.4 — Ticket issues carry order

- `ticket_issues.md`: the planner must return issues in intended
  implementation order — the worker now numbers GitHub issue titles
  `[Task <ticket_id>] <n>/<total> — <title>`, so every issue is traceable to
  its Odoo record and the sequence survives GitHub's list sorting.

## v1.3 — Ticket issue prompt hardening

- `ticket_issues.md`: issues are now always written in English (freelancers
  are not necessarily German speakers), and the rules explicitly forbid
  serializing the `issues` array into a JSON string — a production run failed
  schema validation when the model returned the array as a malformed embedded
  JSON string (unescaped quotes). The tool description carries the same
  instruction; on the code side such validation failures are now classified
  transient (RQ re-plans) instead of failing the run outright.

## v1.2 — Ticket issue planning prompt

- Added `ticket_issues.md`: system prompt for the create-issues flow
  (github-issues handoff). Plans 1–10 GitHub issues from an Odoo ticket's
  title/description and, when present, its completed REVA analysis; splits
  only into independently deliverable pieces, same-language rule, forced
  `submit_ticket_issues` tool call.

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
