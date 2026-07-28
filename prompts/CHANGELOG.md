## v2.16 — A negative about standard Odoo has to be earned

- All four answer-shaped prompts (`support_answer.md`, `ticket_analysis.md`,
  `skills/reva-support-answer.md`, `skills/reva-ticket-analysis.md`) may no
  longer assert that standard Odoo lacks a feature. Ticket 6743 (2026-07-28) was
  told there is "weder eine Einrichtungsoption noch einen Schalter" for marking
  BOM components optional, and that Odoo's Optional Products has "keinen
  fachlichen Bezug zu einer Stückliste" — while Odoo 19 marks a whole quotation
  section optional from the line's ⋮ menu (`sale.order.line.is_optional`,
  `sale_management`), and `cu_sale` explodes a Project BOM into exactly such a
  section. The answer offered to build a feature the customer already owns.
- Two blind spots produced it, and each prompt now names its own: the CLI skills
  run against a clone holding **custom addons only** (Odoo.sh repos gitignore
  `odoo/` and `enterprise/`), and the Messages-API prompts see at most a handful
  of keyword-picked doc sections. Retrieval missing a feature is the expected
  case, not evidence of absence. `standard_coverage: "none"` now requires
  positive evidence; without it the answer is `"unknown"`.
- The support prompts additionally may not explain away something the customer
  has seen. The old answer speculated that the screenshot in the mail thread
  "probably" showed an unrelated feature — it showed the actual feature.
- The two CLI skills are now told the Odoo core source is mounted and to grep it
  (`ticket_knowledge.core_source_param`). Before this, `--add-dir /core/<v>` was
  granted to full reviews and audits only, so the two skills whose whole job is
  "does stock Odoo already cover this?" were the ones that could not look.

## v2.15 — Ticket analysis: the CLI skill states its own output contract

- `skills/reva-ticket-analysis.md` now writes out the `submit_ticket_analysis`
  fields, their enums, and the estimate calibration bands. It previously said
  "matching the `submit_ticket_analysis` schema — the same sections, confidence
  values … as the standard ticket analysis", which the escalated run cannot see:
  the headless CLI gets no tool definition and never receives
  `ticket_analysis.md`. Two paid escalations on 2026-07-27 (analyses 77/78,
  ~$3.80) invented `missing_info[].question` with `confidence: "high"` and an
  `existing_customizations` string, and died in Pydantic validation with nothing
  to show the consultant. `skills/reva-support-answer.md` had listed its fields
  since v2.12 — this brings the sibling skill in line. Guarded by
  `test_ticket_analysis_skill_spells_out_the_output_contract`, which reads the
  required field names straight off the tool schema.
- The calibration bands ride along for the same reason: they lived only in
  `ticket_analysis.md`, so a code-grounded analysis was quoting uncalibrated
  hours.
- Not a prompt change, but the reason this surfaced: a degenerate tool call
  written as `<parameter name=…>` text is now refused in `TicketAnalysisResult.
  summary` and `SupportAnswerResult.answer` (`reva/types.py`). v2.13 removed the
  trigger it was first seen with; the text itself reached a customer's Odoo
  ticket on the ticket path a week later, so the content is now checked.

## v2.14 — Support answers: code is evidence, never output

- Both support prompts: no Python model, field, method, XML view, controller,
  table or file path may appear in `answer`, `cannot_answer_reason` or
  `open_questions`. A code-grounded answer came back with "siehe
  cu_sale/models/sale_order.py, Methode `_action_confirm`" in the body the
  consultant forwards to the customer — unreadable to them, and an
  unnecessary disclosure of how the system is built. This is the rule
  `skills/reva-ticket-analysis.md` already carries, applied to a stricter
  audience: a product owner at least works on the project, the customer does
  not. Same consultant-level carve-out — Odoo apps, settings, features and
  custom **addon** names are allowed; a file path or field name is not.
  `sources` is where the references belong, and it is now a separate internal
  Odoo field the customer never sees, so citing there costs nothing.
- Backed by `find_code_references()`, which records a `code_reference_in_answer`
  ops event when a reference slips through. It does not fail the turn: an
  otherwise good answer with one stray path still beats no answer, and the
  consultant reviews before sending. Deliberately high-precision — Odoo dotted
  model names are not matched, being indistinguishable from "z.B." or
  "Version 19.0" in ordinary prose.

## v2.13 — Support answers: refuse the wrong system, emit null for "no answer"

- Both support prompts (`support_answer.md`, `skills/reva-support-answer.md`):
  grounding that is about a **different system, module or topic** than the
  question is not grounding. Retrieved docs and repository code describe
  whatever project happens to be linked, which is not evidence that they
  answer *this* question — so that case is `cannot_answer`, not a partial
  answer translated out of the wrong codebase. Prompted by a real turn that
  answered an rs2/Mandanten question out of a linked BMD repo. The rule the
  operator asked for, in their words: no answer is better than a bad one.
- `cannot_answer` now says **set `answer` to `null`** — the JSON value, not an
  empty string, not a placeholder. This is a prompt/schema agreement, not a
  wording preference: every property is `required` in a strict tool schema, so
  a non-nullable `answer` left the model no way to express "nothing belongs
  here". Told to leave it empty and unable to, Sonnet 5 degenerated — emitting
  `</antml parameter><parameter name="cannot_answer_reason">` into the field,
  writing `"placeholder"` as the reason, and in one run never terminating
  (16384 output tokens for a 1.1 KB payload, failing the turn). The schema
  side now offers null for `answer` and `handoff.rationale`; Pydantic coerces
  it back to `""`. Measured on the live API against the question that failed:
  0/3 valid calls before, 24/28 at ~800 output tokens after.

## v2.12 — Support answer drafts

- New `support_answer.md`: Messages-API prompt for `reva/support_answerer.py`
  (support-answers feature, spec `docs/superpowers/specs/2026-07-25-support-
  answers-design.md`). Drafts an answer to an Odoo support request via the
  structured `submit_support_answer` tool — a **draft for a consultant to
  review before sending**, never a message posted to the customer directly.
  Same-language rule as `ticket_analysis.md`; obeys the rendered `## Persona`
  system block, treating its `### Content policy` section as hard
  constraints; cites `sources` on `answered`/`partially_answered` drafts; the
  `cannot_answer` contract forbids a caveated draft — state the reason and
  `open_questions` instead. Internal-visibility chatter is fenced separately
  from public chatter in the user prompt with an explicit never-quote
  instruction — it is context only and must never become recognisable in the
  output, the single worst failure this feature can have.
- New `skills/reva-support-answer.md`: the headless-CLI counterpart, used when
  the planner judges the question needs the project's own code. Same output
  contract as `support_answer.md`; repo-aware, so it is CodeGraph-enabled and
  deliberately does NOT receive `review_guidance.md` (findings governance is
  wasted tokens and contradictory guidance for a skill that emits no findings).
- `core_query_planner.md`: new `needs_repo_code` flag — the code-grounding
  gate. `true` only when answering needs this customer's own code or
  configuration rather than the official docs; false whenever unsure, since it
  triggers a full agentic repository pass at roughly 10–30× the cost. Stated as
  independent of `worth_checking`: a question can need the project's code while
  the official docs are irrelevant, and the planner must judge each separately.
- The draft field is `answer` (renamed from `answer_html`) and carries **plain
  text**, not markup: the formatter escapes it — it is model output shaped by
  untrusted customer text, rendered in a consultant's Odoo view — and rebuilds
  paragraphs from blank lines. Both support prompts state this, and a test
  pins the contract in both, since two producers feeding one escaping consumer
  is exactly the shape that drifts. Note `support_turns.answer_html` keeps its
  name: that column stores the rendered fragment and genuinely is HTML.
- New `skills/reva-ticket-analysis.md`: ticket analysis grounded in the
  project's code, on the same planner gate. Its load-bearing rule is that the
  code is **evidence, never output** — the analysis is written for a product
  owner, so no model, field, method, view, or file path may appear in
  `summary`, `missing_info`, or `story_estimates`; the existing
  consultant-level carve-out for Odoo app and custom **addon** names stands.
  CodeGraph-enabled, and excluded from `review_guidance.md` like the support
  skill.

## v2.11 — Structured issue dependencies (builds_on)

- ticket_issues.md: dependencies between issues move from hand-written body
  lines to the structured `builds_on` field (1-based positions in the returned
  array, earlier issues only). The planner must never write sequence
  references like "(1/3)" into titles or bodies — the runner re-orders the
  plan dependency-first and renders the "Builds on (n/total)." line itself
  (was: the model guessed the total and could reference later issues; ticket
  6324 produced "Builds on (1/3)" inside a 4-issue plan with a forward
  reference).

## v2.10 — Existing-customizations grounding

- ticket_analysis.md: new "### 4. Existing Customizations" section (Development
  Estimate renumbered to 5). When a *Retrieved project documentation* system
  block is present (the customer repo's own custom-addon docs), REVA fills
  `existing_customizations` — whether a documented customization already covers
  or is extended by the request. Based ONLY on that block, gated to
  `"unknown"`/empty when absent. Exempt from the no-technical-details rule only
  for custom addon and documented feature names.

## v2.9 — Ticket-level issue typing

- ticket_issues.md: the work-item `type` is classified once from the ticket's
  overall nature and inherited by every issue — sub-tasks of a new-capability
  ticket are `FEAT` even when they touch existing screens; `CR` only when the
  ticket itself requests changing existing agreed behaviour (was: per-issue
  dominant purpose, which typed feature sub-tasks as `CR`).

## v2.8 — Issue-conformance verdicts

- review_guidance.md: new `intent_check` output guidance — one conformance
  verdict per linked GitHub issue (`matches`/`partial`/`does_not_match`/
  `unclear`) when `stated_intent` is present on a full-PR-diff review; walks
  `- [ ]` acceptance-criteria checklists; advisory only, omitted on delta
  reviews.
- reva-diff-review.md / reva-full-review.md / reva-xml-review.md /
  reva-migration-review.md: the "Output format" JSON contract now carries the
  optional `intent_check` array, so the live headless-CLI skill (the actual
  output contract) matches the guidance instead of showing only
  `summary`/`findings`. Excludes reva-delta-review.md by design (delta verdicts
  are dropped at parse).

## v2.7 — Odoo 19 silent-failure pitfalls + estimate calibration

- odoo19.md: six production-verified silent-failure pitfalls from ast-odoo —
  legacy `_sql_constraints` silently ignored (upgraded Minor → Major),
  related-field inverse drop on empty intermediate m2o, status-write-before-
  raise rollback, async-callback terminal-state/staleness guards, global
  `ir.sequence` company binding, boolean `config_parameter` seeding — plus
  migration-script check on column drop/rename.
- ticket_analysis.md (retroactive for the v2 feature wave): lean sections
  (ACs/tests/DoR/DoD removed), Development Estimate section with binding
  AI-assisted calibration anchors; adopt ticket-enumerated use-case splits
  with classification → `kind` mapping; missing_info skips questions the
  ticket already answers.
- ticket_issues.md: split along ticket-enumerated use cases; classification
  markers hint the issue `type`.

## v2.6 — Ticket loop closure

- change_note.md: new Messages-API prompt for merged-PR internal Odoo notes.
- review_guidance.md: ticket_acceptance_criteria guidance for linked Odoo
  ticket acceptance checks.

## v2.5 — Scanner feed

- review_guidance.md: scanner_alerts task-parameter guidance for GitHub
  security alerts as review hints.

## v2.4 — Triage pre-pass

- triage.md: new escalate-only review-depth router for push-triggered diff
  reviews.

## v2.3 — Timesheet wording review

- timesheet_review.md: new Messages-API prompt for Odoo timesheet wording
  review, using structured `submit_timesheet_review` tool output.

## v2.2 — Core knowledge

- ticket_analysis.md: Standard Odoo Coverage section + scoped carve-out.
- core_query_planner.md: new Haiku query planner for ticket retrieval.
- review_guidance.md: standard-functionality category + core-knowledge rules.
- reva-full-review.md / reva-repo-audit.md: core-knowledge steering notes.

## v2.1 — Review-quality pass: examples, self-verification, evidence anchors

- Removed the dead Messages-API review prompts (`system.md`, `diff_review.md`,
  `deep_review.md`) and the `PromptBuilder.build_system_blocks` /
  `build_user_prompt` methods they fed. The CLI is the only review path and
  assembles its prompt directly (`review_guidance.md` + `odoo19.md` + skills);
  those files carried a stale `submit_review` contract and a self-set
  `risk_level` instruction contradicting `review_guidance.md`. `PromptBuilder`
  now owns only versioning + drift hashes.
- Single-sourced the "Team configuration" block (`custom_instructions` /
  `muted_categories` / `team_review_preferences` handling) into
  `review_guidance.md` — it was copy-pasted verbatim into five skills.
  `review_guidance.md` is prepended to every skill, so behaviour is unchanged;
  skills now carry only mode-specific deltas.
- `review_guidance.md`: new "Verify before you write" section — before emitting a
  finding the model must re-Read the cited lines (not count diff hunk lines),
  Grep to substantiate any claim of absence, and check the parent/framework
  method for "missing handling" findings.
- `review_guidance.md`: confidence scoring rewritten to ask for **honest** scores
  — the worker now enforces the 0.7 reporting floor in code
  (`Reviewer.MIN_CONFIDENCE`, `findings_dropped_low_confidence` telemetry), so
  the prompt no longer trains the model to inflate borderline findings to 0.7.
  The per-skill "keep only findings ≥ 0.7" steps now point at the verification
  pass instead.
- `review_guidance.md`: new "Summary contract" section — the `summary` now has a
  defined 3-part shape (what the change does, the top concern or "none", and what
  was verified clean), with the "what was verified" line mandatory on clean
  reviews so an empty findings list still demonstrates the work done. All six
  skills' summary placeholders point at it.
- `odoo19.md`: assigned an explicit severity to every finding-producing rule that
  lacked one (`with_context` misuse, `search_count` limit, `_search_display_name`,
  record-rule OR workarounds, explicit `inherit_id`, `<card>` Kanban, `_read_group`
  signature, `mapped()` on large recordsets, file naming, Python-3.12 type
  patterns) so severities stop drifting run-to-run. Fixed the JSONB-translations
  version (was "17+", now "16+" to match the migration skill and actual Odoo
  history) and gave it **Major**.

## v2.0 — Learned team preferences on the review path

- All five review skills' "Team configuration" section gains a third optional
  param, `team_review_preferences`: a distilled per-repo summary of what the
  team has accepted/dismissed in past reviews (Tier 3 feature B). It adjusts
  prioritization within the repo and can never suppress a security/bug finding
  or override severity/output rules. Injected only when an active learned-memory
  version exists and the repo hasn't set `learned_memory: false`.

## v1.9 — Team configuration on the review path + verifier re-pricing

- All five review skills gain a "Team configuration" section. Reviews now
  receive two optional nonce-fenced params: `custom_instructions`
  (team-authored guidance from `.claude-review.yml`, previously dead on the
  review path — it reached only the Messages-API ticket/reply prompts) and
  `muted_categories` (categories a trusted user muted — the model is told not
  to report them up front; the post-hoc drop stays as enforcement backstop).
  Neither overrides severity definitions, security rules, or the output format.
- `finding_verifier` (code-side, not in this prompt set): both verifier system
  prompts now note the file content may be a ±150-line excerpt around the
  cited line, with its absolute range labelled above the fence.

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
