# Triage escalation (escalate-only pre-pass) — design

**Date:** 2026-07-05
**Component:** `cu_reva` — new `reva/triage.py` + `prompts/triage.md`, `Reviewer` wiring, `ReviewResult`/`review_runs` stamp, worker settings, RepoConfig kill switch.
**Status:** Design approved (Q&A with Joseph, 2026-07-05), pending implementation plan.

## Problem

Review depth is decoupled from content risk: a routine one-liner and an
ACL-touching rewrite both get the default `diff` review unless a human types
`/deep-review`. The roadmap's Tier-4 "tiered triage→escalate routing" was
never planned. Joseph's decision narrows it to the safe half: the cheap
pre-pass may only **escalate**, never skip or downgrade — no review can be
lost to a cheap model's misjudgment.

## Context (verified 2026-07-05)

- `Reviewer.execute` (worker/worker/reviewer.py): diff is fetched/filtered,
  `_select_skill` picks the skill from the final diff (~line 433), the model
  is picked from `params.review_mode` (~line 452). The escalation must land
  before both.
- `params.trigger_event` distinguishes push events (`opened`, `synchronize`,
  `reopened`, `ready_for_review`) from human-intent triggers (`comment`,
  `manual`, `manual_requeue`).
- `Reviewer.__init__` has no Messages-API client today (only the CLI runner
  + verifier); `WorkerContext.claude` exists.
- The Haiku tier (`REVA_VERIFY_MODEL`) + forced-tool + fencing patterns are
  established (verifier, ticket planner). Ops-event invariant is live.
- The prompt drift guard hashes the prompt set — adding `prompts/triage.md`
  requires a CHANGELOG version bump.

### Locked decisions

1. **Escalate-only:** `diff`/`diff-all` may be upgraded to `full`, or to
   `deep` for security-critical shapes (auth/ACL/`sudo()`/raw SQL/migration
   scripts). Never skip, never downgrade, never touch `full`/`deep` requests.
2. **Push-triggered reviews only** — `/review`, `/deep-review`, requeues are
   explicit human intent and bypass triage entirely.
3. **Fail-open** to the original mode on any error, with an ops event
   (`component="triage"`).
4. **Staged rollout:** global `REVA_TRIAGE_ENABLED` default **false**;
   per-repo `.claude-review.yml triage: false` kill switch.
5. **Measurable:** the decision is stamped (`review_runs.triage_escalation`
   = `"full"|"deep"|NULL`, reason logged; Haiku spend ledgered as
   `kind="triage"`), so hit-rate and cost are queryable and the golden-PR
   harness can score it later.

### Explicitly out of scope

- Skipping/downgrading (revisit only with golden-PR evidence).
- Triage of audits, tickets, replies.
- TUI surface beyond the stamped column (a reviews-list badge is a
  follow-up; deferral conscious per the TUI principle).

## Design

**`reva/triage.py`:** `TriageDecision(escalate: Literal["none","full","deep"], reason: str)`;
`decide(claude, prompts_dir, diff, changed_files, current_mode) -> tuple[TriageDecision, float]`
(decision, cost) — one Haiku call, forced tool `submit_triage`
(`strict: true`), system prompt from `prompts/triage.md` (cache-controlled),
user prompt = changed-file list + diff stats + **nonce-fenced diff excerpt**
(cap ~24k chars) with the standard untrusted-data framing. Never raises:
errors return `("none", reason="error: …")` and the caller records the ops
event. An explicit rule in the prompt: *when uncertain, do not escalate* —
the default path is the status quo.

**Wiring (`Reviewer.execute`):** new constructor params
`claude: ClaudeClient | None = None`, `triage_enabled: bool = False`.
Before skill selection:

```
if triage applies (enabled globally, repo not opted out, mode in diff/diff-all,
                   trigger is a push event, claude is wired):
    decision, cost = triage.decide(...)
    ledger cost (kind="triage")
    if decision.escalate != "none":
        effective_mode = decision.escalate      # feeds skill + model selection
        log review_triage_escalated + stamp onto the result
```

`ReviewResult` gains `triage_escalation: str | None = None`; the runner
persists it to a new nullable `review_runs.triage_escalation` column
(migration: next free number). Skill/model selection reads the effective
mode; everything downstream (caps, debounce rows, Check Run text) is
unchanged — the escalated review is simply a full/deep review.

**Prompt (`prompts/triage.md`):** role (risk router, not reviewer), the
escalate-only contract, `deep` criteria (security surface: ACLs/ir.rule,
`sudo()`, raw SQL, migration scripts, auth/controller exposure), `full`
criteria (cross-module blast radius, model/mixin surgery, big moves the diff
view can't judge), uncertainty rule, fenced-data rule. CHANGELOG bump + drift
test update.

## Error handling

| Case | Behavior |
|---|---|
| Haiku call fails/times out/invalid tool output | proceed at original mode; ops event `triage/decide_failed` |
| Budget cap reached | triage is skipped before the call (reuse `budget_exceeded` check result already computed for the review path); no ops event (expected) |
| Repo kill switch / non-push trigger / full-deep already | triage not invoked at all |

## Testing

Pure `decide()` with fake Claude (fencing, forced tool, cap, error → none);
`Reviewer.execute` wiring matrix (enabled/disabled, kill switch, trigger
gating, escalation changes skill+model, stamp on result, spend ledgered,
fail-open + ops event); writer/migration for the stamp column; prompt-set
version test updated. Staging gate: enable on one repo, watch
`triage_escalation` hit-rate for a week before fleet-wide default-on.
