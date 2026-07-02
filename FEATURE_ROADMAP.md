# REVA — Feature Roadmap

A prioritized, code-grounded roadmap of features that would extend REVA's skillset as
an Odoo-focused PR review platform. Each item was proposed against the actual codebase
and adversarially verified (is it already built? is the stated problem real?).

Legend — **Effort**: S / M / L · **Value**: H / M / L.
`🔓` = foundational, unlocks dependent features.

---

## The through-line: REVA has dormant infrastructure

> **STATUS (2026-07-02): RESOLVED — this section is historical.** Every gap below has since
> shipped: `review_feedback` is written (`api/app/routes/webhooks.py:385`, `writers.record_feedback`),
> `prompt_versions` is written (`writers.register_prompt_version`), delta reviews feed prior
> findings back via `already_reported` (prompt v1.6, `prompts/skills/reva-delta-review.md`),
> `reva-risk-*` PR labels are set (`worker/worker/runner.py:69,421`), and the delta-resolution
> verdict is persisted to the finding-outcome ledger (`db/migrations/015_finding_outcome.sql`).
> Kept for context on why Tiers 0–3 exist.

The strongest finding is that REVA already **built the machinery for a learning loop and
never wired it up**. Several independent gaps trace back to this:

- The PR review body says *"React with 👍 or 👎 to help me improve"* (`reva/review_formatter.py:252`),
  the `review_feedback` table exists (`db/migrations/002_feedback.sql`), and the `feedback_stats`
  query + `GET /api/v1/metrics/feedback` endpoint read it — **but nothing ever writes a row.**
  Every approval-rate metric is null forever.
- `prompt_versions` table + ORM (`db/migrations/003_prompt_tracking.sql`, `reva/db/models.py:519`)
  exist but are **never written** — a changed-but-unbumped prompt goes undetected.
- `get_open_findings_for_pr()` exists, but delta reviews **don't feed prior findings back to the
  model**, so they re-emit near-identical comments.
- `risk_level` is computed and stored but **invisible** at PR-list triage time.
- The delta-resolution loop computes a fix-verified verdict per finding
  (`runner.py:_verify_and_resolve_findings`) and **throws it away** — that's free ground-truth labels.

**Activating this loop is the highest-leverage theme.** It is the spine the self-improvement
tiers hang off.

---

## Tier 0 — Quick wins (ship now; standalone, S effort, high payoff)

| Feature | E/V | Summary |
|---|---|---|
| **Trivial-diff short-circuit** | S/H | Detect whitespace/comment/import-only diffs in `diff_utils.py` and skip the paid Claude call entirely. |
| **Per-repo severity gating** | S/H | `block_on_severity` in `.claude-review.yml`; today `compute_check_conclusion` hard-fails on any major/critical with no override. |
| **Risk labels on the PR** | S/M | Set a mutually-exclusive `reva:risk-{critical…low}` label from the already-computed `risk_level` so leads triage from the PR list. |
| **Deterministic Odoo severity calibration** | S/M | `odoo19.md` documents intended severities (raw `cr.execute`→critical, etc.) but the model assigns freely; a small rule-table floor/cap enforces them. |

> Detailed implementation plan: [`docs/tier0-plan.md`](docs/tier0-plan.md).

## Tier 1 — Activate the loop (foundational; unlocks Tier 3)

| Feature | E/V | Summary |
|---|---|---|
| **Feedback capture** 🔓 | M/H | Subscribe to reaction + thread-resolved webhooks; write 👍/👎/dismissals into the dormant `review_feedback` table. Unblocks everything in Tier 3. |
| **Per-finding outcome ledger** 🔓 | M/H | Persist the *already-computed* fix-verified verdict from the delta-resolution loop → implicit ground truth at zero extra Claude cost. |
| **prompt_versions hash write** | S→M/H | On startup, hash the assembled prompt files into the dead `prompt_versions` table; alert on unbumped drift. (S subset first; full eval-gate is M.) |

> Detailed implementation plan: [`docs/tier1-plan.md`](docs/tier1-plan.md).

## Tier 2 — Review intelligence & Odoo depth (the actual "skillset")

| Feature | E/V | Summary |
|---|---|---|
| **Intent-grounded review** | M/H | Resolve `Closes #N` / Odoo ticket → feed the issue body + existing `ticket_analyses.acceptance_criteria` (fenced) so REVA can ask *"does this diff do what was asked?"* |
| **Test-coverage gate** | M/H | `reviewer.py` fetches `changed_files` then discards all but `filename`; derive per-module "new logic, no `tests/` change" and fire the existing `test` category on the cheap diff path. |
| **XML/QWeb review skill** | M/H | REVA *wrote* view-review rules in `odoo19.md` but blanket-strips all `.xml`; a `reva-xml-review` skill applies them (xpath/`inherit_id` targets, CSP, `t-esc`→`t-out`). |
| **Security-model consistency** | M/H | New `_name` in a diff → check `ir.model.access.csv` + `ir.rule` coverage in the same module. Unprotected new models are an access-control hole. |
| **`__manifest__.py` validator** | M/H | Structural checks LLM reasoning misses: used-but-undeclared `depends`, non-existent data files, security-before-views ordering, version format. |
| **Migration-safety skill** | M/H | Path-route `migrations/<ver>/{pre,post}-migrate.py` to a dedicated skill — highest-blast-radius code an Odoo team writes (raw SQL on prod data). |
| **Delta-aware finding suppression** | M/H | Show the delta model its still-open prior findings so it stops re-emitting comments on issues already flagged. |
| **Translation/.po hygiene** | L/M | Selectively include `i18n/*.po` when a PR touches `_()` strings; catch format-specifier mismatches (runtime crashes). |
| **Second-pass self-critique** | M/M | Re-verify major/critical findings against the cited code region before posting (budget-bounded) — cuts false positives on diff-only reviews. |

## Tier 3 — Self-improvement (needs Tier 1 data)

| Feature | E/V | Summary |
|---|---|---|
| **Per-repo learned memory** | L/H | Derive a "what this team accepts/rejects" block from feedback and inject it into the prompt — stop repeating dismissed findings. |
| **`/dismiss` & `/mute <category>` commands** | M/H | Structured, zero-cost replies on inline comments (vs. today's paid conversational reply); the cleanest source for learned memory. |
| **Golden-PR replay harness** | L/H | Curated Odoo PRs with annotated expected findings → precision/recall scoring; makes prompt/model changes defensible. |
| **A/B comparison runner** | M/M | Run golden PRs through model-A vs B / CodeGraph on-off — replaces one-off folklore measurements with evidence. |
| **Quality dashboard** | M/M | FP-rate & resolved-by-fix trends by category/repo/prompt-version in the TUI. |
| **RAG over prior findings** | L/M | pgvector index to down-rank findings similar to previously-dismissed ones across PR history. |
| **Per-author trust digest** | S/M | Add a "review trust" section to the existing weekly Google Chat report. |

## Tier 4 — Lifecycle & scale

| Feature | E/V | Summary |
|---|---|---|
| **Chunk oversized PRs** | L/H | Batch by file/addon instead of hard-declining `Diff too large` — large refactors currently get zero review. |
| **Correct delta base on force-push/rebase** | M/H | Validate `last_review.head_sha` is an ancestor of the new head before trusting `get_compare_diff`. |
| **Ticket→issue→PR traceability** | M/H | Link a PR back through `ticket_issue_runs.issues` to the originating Odoo ticket → acceptance-criteria coverage. |
| **Scheduled periodic re-audits** | M/M | `maybe_enqueue_audits` loop (mirrors eviction/purge), drift-only issue creation via existing dedup markers. |
| **Audit-finding lifecycle** | M/H | Auto-close stale `reva-audit` issues when a finding disappears; SLA-age alerts on the rest. |
| **Base-moved re-review** | L/M | Re-review open PRs when their base branch advances (stacked-branch workflow). |
| **Tiered triage→escalate routing** | M/H | Cheap Haiku pass decides SKIP / diff / escalate-to-Opus — decouples model cost from content risk. |
| **Per-repo / per-author budgets** | M/H | Today only one global 24h cap; one runaway repo starves everyone. |
| **Priority queue** | S/M | Split `interactive` vs `batch` RQ queues so a `/review` doesn't sit behind a long Opus audit. |
| **Batched delta verifier** | M/M | `_verify_and_resolve_findings` re-sends full file content per finding (≤20×); batch to one call per file. |

## Tier 5 — Integrations & ops

| Feature | E/V | Summary |
|---|---|---|
| **Feed scanner output into the review** | M/H | REVA's CI runs semgrep/gitleaks/Dependabot but the review is blind to them — pass alerts as triage context. |
| **Check Run annotations + status badge** | M/M | Emit findings as annotations (surface in the Checks UI) + a public badge; better branch-protection/merge-queue integration. |
| **Pluggable notifiers (Slack/Teams)** | M/M | `reva/notifications.py` is hard-wired to Google Chat; a `Notifier` Protocol (matching the existing `GitHubReader`/`RepoLookup` patterns). |
| **Prometheus `/metrics`** *(matures deferred B2/B3)* | M/H | Queue depth / spend rate / budget headroom are unobservable without log-grep today. |
| **Structured error tracking** *(matures parked B1)* | M/M | Errors are classified (`PermanentError`, `error_class`) but not searchable/aggregated — route through `logging.py` to Sentry/GlitchTip. |

---

## Exclusions & decisions honored

- **Dropped by verification:** *computed-field `@api.depends` analyzer* — the `odoo19.md` rule
  already fires in full/audit modes and a real analyzer needs AST + field-schema data CodeGraph
  v0.9.8 doesn't expose; gain judged moderate, not high.
- **Team-rejected, not re-proposed** (per `HANDOFF.md` decisions): committable suggested-fixes
  (juniors learn by doing) and change-"walkthrough" summaries.
- **Formerly-known P0 bugs — already fixed (verified 2026-06-14):** comment-commands on unknown PRs
  (fetch+upsert on a DB miss, `004cd5c`) and the `deep→diff` downgrade (CORR-7, `_MODE_PRECEDENCE`
  guard in `upsert_pending_review`, `11e601a`). Both have passing tests; no longer prerequisites.

## Recommended near-term sequence

1. **Tier 0** — four standalone quick wins, immediate cost/UX payoff (see `docs/tier0-plan.md`).
2. **Tier 1** — the feedback loop; cheap, and the gate on the entire Tier-3 self-improvement story.
3. **Tier 2** — pick by what your teams hit most; start with **XML/QWeb review** (largest
   *unaddressed* category — REVA throws away every view change today).
