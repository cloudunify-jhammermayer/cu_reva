# Support Answers — Persona-Shaped Draft Replies to Odoo Support Requests

**Status: 📐 DESIGNED (2026-07-25). Not implemented.**

Odoo sends a support request (question text + chatter thread, optionally tied
to a GitHub project). REVA answers it — grounded in the Odoo `/core` knowledge
base, the customer repo's own docs, and the repo's code — in a tone configured
per project, and writes the answer back as a **draft for the consultant**, not
a message to the customer.

This is REVA's first output shaped for an external reader. Every existing
output (review findings, ticket analysis, timesheet verdicts, change notes) is
staff-facing; a wrong finding costs a consultant five seconds. A wrong support
answer, once forwarded, reaches the customer. That asymmetry drives the
draft-not-send decision below and the hard `cannot_answer` contract.

Roughly 70% of the pipeline already exists: the ticket-analysis path
(`api/app/routes/v1/ticket_analyses.py` → `worker/worker/ticket_runner.py`) is
the same shape, and `reva/ticket_knowledge.py::build_ticket_knowledge` is
already the retrieval layer this needs.

## Decisions (locked with Joseph 2026-07-25)

- **Draft, not send.** REVA writes to a new HTML field on the Odoo record via
  the existing `write_field` callback; a consultant edits and sends. No new
  outbound contract, no customer-visible chatter post. Auto-send stays open as
  a later decision once there's outcome data.
- **Consultant button press only.** No auto-trigger on customer chatter reply.
  Keeps volume predictable and avoids drafting an answer to "thanks, that
  worked!". Odoo can flip this later without a REVA change.
- **Persona lives in REVA, keyed on `repo_full_name` text.** Not
  `.claude-review.yml` (dev-owned, needs a PR to change tone, and doesn't
  exist for project-less requests) and not FK'd to `repositories` (a support
  request can name a repo REVA has never received a webhook for —
  `repo_doc_sections` already keys this way). Resolution is **per field**:
  `default` row < repo row (NULL fields inherit) < Odoo `persona_context`.
- **`persona_context` is consultant-authored and additive.** Maintained by
  staff on the Odoo record, layered on top of the repo persona rather than
  replacing it, and placed in the system block as trusted content. It never
  overrides the structured knobs. If this ever becomes customer-editable in
  Odoo, it must move to a nonce-fenced user block — flagged in the contract.
- **Hybrid persona schema.** Structured knobs (`language`, `formality` — the
  *Sie/du* axis, `technical_depth`, `length`, `salutation`, `sign_off`) plus
  free-text `style_notes`, plus a **separate** `content_policy` field. Policy
  ("never quote prices", "never commit to a delivery date") renders as a hard
  constraint in the prompt rather than getting lost in style prose.
- **Answer in the language the request is written in**, matching the existing
  `prompts/ticket_analysis.md` rule. A persona may pin a language. The docs
  indexes are English-tokenized and the planner already emits English search
  terms, so retrieval is unaffected.
- **Two grounding depths, chosen by the planner.** `plan_core_queries` already
  runs first and already decides `worth_checking`; extend it to also emit
  `needs_repo_code`.
  - **No** → Messages API only, with `build_ticket_knowledge` unchanged
    (`/core` block + repo-docs block). Fast, cheap, and prompt-cached across
    turns of the same thread.
  - **Yes** → one headless-CLI run against the worker clone with a new
    `reva-support-answer` skill, which produces the whole answer.

  One paid path per turn either way, never both. Keyword retrieval was the
  first design here and it is the wrong tool for the `cannot_answer` bar: a
  `git grep` that finds nothing is weak evidence (wrong terms, German vs
  English naming, a different abstraction), whereas an agent that explored can
  say "no, and here's where it would live". Support questions are also
  routinely multi-hop — field → computation → custom-addon override → view —
  which one round of term-matching cannot follow. The CLI additionally gets
  CodeGraph, and its output contract is identical (`ClaudeCodeRunner.review()`
  returns a `ClaudeResponse` with `tool_use_input` from the tool-schema JSON),
  so the formatter and callback are unaffected.
- **A linked repo is mandatory for ticket analysis — enforced in Odoo, not
  REVA** (locked 2026-07-26). Odoo blocks submission without a GitHub project,
  so `github_url` stays **nullable in REVA's contract**: no breaking schema
  change, no coordinated release, no legacy-row problem. When it is absent
  anyway, REVA degrades rather than refuses — it sticks to Odoo `/core`
  knowledge: no repo-docs block, no code escalation, *Existing Customizations*
  reported as unknown (matching the existing `standard_coverage: "unknown"`
  convention), and an ops event so the degradation is visible.
- **REVA does not book time in Odoo** (locked 2026-07-26). Odoo defines the
  cost and observes when an analysis is actually used, so it books locally.
  A REVA→Odoo booking callback would add a contract, an idempotency guard
  against requeue double-booking, and a billing artifact REVA can't correctly
  trigger — REVA can see delivery, not use.
- **The same gate applies to ticket analysis** (locked 2026-07-26). It is not
  support-only. Ticket analysis already calls `plan_core_queries`, so the gate
  is written once and read by both runners. The rationale is the same in both
  places — the *output* is business-level, the *evidence* often isn't:
  *Existing Customizations* is grounded only in addon READMEs today, *Standard
  Odoo Coverage* can be wrong when the customer already overrode the stock
  behavior, and *Missing Information* lists gaps that stop being gaps once the
  field is visible in code. Two divergent grounding strategies across two
  Odoo-facing analysis paths would be two things to tune and two ways to be
  wrong. Ticket analysis keeps its no-code-level-detail output rule, which
  becomes a harder instruction to hold once the model has read the code — see
  the plan's Task 10 Step 4.
- **The CLI path takes the repo lock, and that's accepted.**
  `ClaudeCodeRunner.review()` calls `_scrub_clone` and writes a temp output
  file into the working tree, so unlike `two_tree_diff` it cannot be lock-free.
  A support request can therefore queue behind a PR review or audit of the same
  repo, or requeue as `TransientError` if the lock is busy past the wait
  budget. Acceptable here: triggering is a consultant button press, the output
  is a draft nobody is watching a spinner for, and volume is low.
- **Cold cache warms synchronously**, inside the same lock the CLI run already
  holds: `ensure_repo(owner, name, None, token)` clones/fetches and resets to
  `origin/HEAD`, then `review()` runs. The first support request for a repo
  pays the clone; later ones only pay the fetch. A repo without the GitHub App
  installed can't be warmed — degrade to docs-only, mark the draft as not
  verified against project code, and record an ops event.
- **Support activity refreshes the cache entry** so an actively-supported but
  quiet repo isn't evicted by `REVA_REPO_CACHE_TTL_DAYS` (30) and re-cloned.
- **REVA holds thread state; Odoo re-sends the full chatter each turn.** REVA
  owns the thread of its own Q&A turns and replays them into the prompt.
  Delta bookkeeping in the addon ("everything since chatter id N") drifts the
  moment a message is edited, deleted, or backdated, with no cheap way to
  detect it. Full snapshot in, REVA-side turn history here, is self-healing.
- **Chatter carries an explicit `visibility` flag.** Internal notes go into the
  prompt in a **separately fenced block** with a never-quote instruction —
  they frequently contain the actual answer ("fixed in 2.3, not deployed yet"),
  so dropping them throws away the best source, but an unmarked internal note
  reaching a customer is the worst failure this feature can have. Needs a test.
- **`cannot_answer` produces no drafted prose.** The field gets the reason plus
  `open_questions` — what REVA would need in order to answer. A caveated draft
  the consultant must fact-check costs more to check than to write.
- **Answered drafts cite sources** (core doc, repo doc, repo file) so the
  consultant can verify before forwarding.
- **A request can be a change request *and* have an answer.** Classification
  emits `request_kind` plus a `handoff` hint; the two paths cooperate rather
  than compete. See *Classification* below.
- **Shared budget.** Draws on the existing per-instance `daily_budget_usd` and
  the global rolling cap. No separate budget line for now.
- **Attachments: add `.md`, defer images.** `.md` is a one-line addition to
  `_ALLOWED_EXTENSIONS` in `reva/attachment_text.py:26` through the existing
  UTF-8 text path. Images are deferred — they'd require extending
  `ClaudeClient.review()` to accept content blocks (it sends a bare string
  today, `reva/claude_client.py:58`), a per-image token budget, and an
  image-specific injection guard, since nonce-fencing can't wrap an image.
- **Odoo fields carry no `x_` prefix.** The prefix is only required for
  manually created (Studio) fields — `ir.model.fields` rows with
  `state='manual'`. Fields declared in Python in a real addon don't need it.
  REVA requires no change either way: `field_name` always flows through from
  the request, and `x_reva_analysis` appears only in samples, tests and docs.
- **Persona editing happens in the TUI**, alongside the writes it already
  does. The "read-only client" wording in the docs is **stale, not an
  invariant**: `tui/internal/api/client.go` already ships `TriggerAudit`,
  `AddRepo`, `Requeue` / `RequeueTicket` / `RequeueIssueRun`,
  `CreateOdooInstance`, `RotateOdooInstanceKey`, `SetOdooInstanceActive`, and
  `DeleteOdooInstance`. Correcting that prose is mandatory in this change
  (working principle #1: code wins over docs).

## Shape

### Inbound contract

`POST /api/v1/support-request` — instance-key gated, `202 Accepted`,
fire-and-forget, dedup on a pending row per (instance, ticket, model, field),
RQ enqueue with retry. Mirrors `submit_ticket_analysis`.

```jsonc
{
  "ticket_id": 4711,
  "model_name": "helpdesk.ticket",
  "field_name": "reva_support_answer",
  "thread_id": null,                   // null on turn 1; REVA's id thereafter
  "subject": "Rechnungslauf bricht ab",
  "question": "…",                     // turn 1: the ticket description
  "github_url": "https://github.com/acme/widgets",   // nullable → default persona
  "persona_context": "…",              // nullable, consultant-authored, additive
  "chatter": [
    { "id": 98123, "posted_at": "2026-07-25T09:14:00Z",
      "author": "Maria Huber", "author_kind": "customer|internal|system",
      "visibility": "public|internal", "body": "…" }
  ],
  "attachment": null                   // existing Attachment model + .md
}
```

`github_url` is format-validated at accept time (no reachability probe),
matching ticket-analysis. Contract entry in `reva/odoo_contracts.py::CONTRACTS`
+ `_inbound_models()`, `contracts/` regenerated, synced to ast-odoo with the
pin bumped.

### Storage

- `043_personas.sql` / `reva/db/models.py::Persona` — `scope`
  (`'default'|'repo'`), `repo_full_name` (NULL for default; partial unique
  index where `scope='repo'`), `language`, `formality`, `technical_depth`,
  `length`, `salutation`, `sign_off`, `style_notes`, `content_policy`,
  `active`, timestamps.
- `044_support_threads.sql` / `SupportThread` + `SupportTurn`:
  - `support_threads` — `odoo_instance_id`, `ticket_id`, `model_name`,
    `github_url`, `persona_snapshot` (JSON, what actually applied),
    `status`, `created_at`, `last_turn_at`; UNIQUE (instance, ticket, model).
  - `support_turns` — `thread_id`, `seq`, `question`, `answer`,
    `result_structured`, `request_kind`, `answer_status`, `grounding_level`,
    token/cost columns, `callback_sent_at`, `callback_error`.

Both idempotent, `id BIGSERIAL PRIMARY KEY`, matching the existing files.

### Worker

`worker/worker/support_runner.py` — reset status → instance budget check →
resolve persona → plan (`plan_core_queries`, extended with `needs_repo_code`)
→ branch:

- **Docs path**: `build_ticket_knowledge` blocks → `reva/support_answerer.py`
  (Messages API + tool schema).
- **Code path**: `repo_lock` → `ensure_repo(owner, name, None, token)` →
  `ClaudeCodeRunner.review(skill="reva-support-answer", …)`.

Then `reva/support_formatter.py` → persist → `write_field`. Idempotent resume
on a completed turn, same as `ticket_runner`.

Both branches return a `ClaudeResponse` carrying the same tool-schema JSON, so
everything downstream of the branch is shared.

`prompts/skills/reva-support-answer.md` is a new **repo-aware** skill: persona
block, fenced chatter (public and internal split), prior turns, the retrieved
`/core` block, and the output contract. Being repo-aware, it joins the
CodeGraph allowlist alongside full/deep/audit — CLAUDE.md's skill-routing
paragraph needs the addition. Skill selection is **not** `Reviewer._select_skill`;
this path picks its skill directly, since there is no diff to inspect.

No repo App installation (`get_repo_installation_id` fails) → the code path is
unavailable: fall back to the docs path, mark the draft as not verified against
project code, and record an ops event.

### Output schema

```jsonc
{
  "request_kind":  "question | change_request | bug_report | mixed | other",
  "answer_status": "answered | partially_answered | cannot_answer",
  "answer":   "…",                  // empty when cannot_answer
  "cannot_answer_reason": "…",           // required when cannot_answer
  "open_questions": ["…"],
  "sources": [{ "kind": "core_doc|repo_doc|repo_code", "ref": "…", "title": "…" }],
  "handoff": { "suggest_analysis": bool, "suggest_issues": bool, "rationale": "…" },
  "language": "de|en",
  "confidence": 0.0
}
```

### Classification and the handoff

`StandardCoverage` in the existing ticket analysis already computes "does stock
Odoo cover this?" — which *is* the answer to most feature requests. So a
change request with an answer is the common case, not an edge case:
`request_kind: "mixed"`, `answer_status: "answered"` (often "standard Odoo
covers this, here's how"), `handoff.suggest_analysis: true`.

Odoo side, on `helpdesk.ticket` / `project.task`:

| Field | Type | Purpose |
|---|---|---|
| `reva_support_answer` | Html | the draft (target of `write_field`) |
| `reva_support_status` | Selection | `pending / answered / partial / cannot_answer / failed` |
| `reva_support_kind` | Selection | drives the consultant's next action |
| `reva_support_request_id` | Integer | staleness guard, like `analysis_id` in `reset_status` |
| `reva_support_thread_id` | Integer | echoed back on follow-ups |

Buttons: **Ask REVA** (turn 1) and **Ask REVA again** (same `thread_id`). When
`reva_support_kind` is `change_request` or `mixed`, the form shows a banner
pointing at the existing *Analyse ticket* / *Create issues* actions — the
handoff made visible, no new mechanism.

### API + TUI

- `GET /api/v1/support-threads` (+ `/{id}` with turns) — read, master key.
- `GET/POST/PATCH /api/v1/personas` — **master key only**, never the instance
  key. This is the write surface the TUI drives.
- New TUI tab: threads, turn status, `request_kind`, `cannot_answer` reasons,
  grounding level (docs vs code), and persona editing — following the existing
  write patterns (`AddRepo`, `CreateOdooInstance`).
- **Mandatory doc correction.** Three places still describe the TUI as
  read-only and are contradicted by `tui/internal/api/client.go`: CLAUDE.md
  working principle #5 (line 64), the architecture pipeline paragraph
  (line 106), and `tui/README.md` line 3.

## Prerequisites

- ✅ **Shipped 2026-07-25.** `core_knowledge.search_docs` now uses OR-of-terms,
  matching `search_repo_docs`. A single `plainto_tsquery` ANDed all ~13 planner
  terms, demanded they all appear in one 2000-char section, and near-never
  matched — silently emptying *Standard Odoo Coverage* on most tickets.
- ✅ **Shipped 2026-07-26 (Phase 0).** Three pre-existing gaps the CLI
  escalation would have walked straight into: the ticket enqueue timeout now
  derives from `REVIEW_JOB_TIMEOUT` (300s would SIGKILL an escalated run
  mid-payment, and `_RETRY` would re-pay twice more), `_STALE_PENDING` is
  derived from it so a stale-requeue can't run beside a live job, requeue
  replays `github_url` (it was dropped, silently downgrading grounding) and
  409s on retention-purged rows, and the main analysis call is now recorded in
  the `claude_spend` ledger — the global rolling cap reads only that ledger,
  so the priciest leg of the ticket path was invisible to it.

## Deferred

- **Images on support requests** — needs `ClaudeClient.review()` to accept
  content blocks, a per-image token cap, and an injection guard that works
  without nonce-fencing.
- **Auto-trigger on customer chatter reply** — Odoo-side change, revisit once
  draft-quality data exists.
- **Auto-send above a confidence threshold** — the threshold is a guess until
  there's outcome data on how often drafts are edited before sending.
- **Renaming `x_reva_analysis`** — Odoo-side migration, no REVA change.
- **Learning from edited drafts** — the `review_feedback` / learned-memory
  pattern applied to support answers.
