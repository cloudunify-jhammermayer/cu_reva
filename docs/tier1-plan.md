# Tier 1 — Implementation Plan (the feedback loop)

The three Tier-1 features from [`FEATURE_ROADMAP.md`](../FEATURE_ROADMAP.md). Tier 1 *activates the
dormant feedback loop* — REVA already built the read side of a learning system and never wrote to it.
Each plan below was drafted against the real code (and the GitHub webhook docs) and then adversarially
verified. The `⚠️ Verification fixes` boxes are corrections that **must** be applied during
implementation. Line anchors are as of commit `0d27ed2`; reconfirm before editing.

## Headline finding: there is no reaction webhook

The roadmap glibly said *"subscribe to reaction webhooks."* **That is unbuildable** — verified against
GitHub's webhook docs: GitHub fires **no** webhook when a user adds a 👍/👎 reaction to a PR review
comment; reactions are REST-only (`GET /repos/{o}/{r}/pulls/comments/{id}/reactions`). So the literal
promise in the review body (*"React with 👍 or 👎 … to help me improve"*, `review_formatter.py:252`) is
**not capturable by webhook**.

The real, webhook-deliverable accept signal is the **`pull_request_review_thread`** event (actions
`resolved` / `unresolved`) — it exists, carries the thread + its comments, and needs only the
`Pull requests: Read` permission REVA already holds. Tier 1's feedback capture is built on that, plus the
free verdict the worker already computes. This is exactly the kind of phantom-capability the verification
pass exists to catch.

| # | Feature | Effort | Net effect |
|---|---|---|---|
| 1 | [Feedback capture](#1-feedback-capture) | **M** | `review_feedback` finally gets rows; `feedback_stats` / `/metrics/feedback` return real data. |
| 2 | [Per-finding outcome ledger](#2-per-finding-outcome-ledger) | **S** | Persist the already-computed fix-verified verdict → labeled ground truth at zero Claude cost. |
| 3 | [Prompt-version registry + drift guard](#3-prompt-version-registry--drift-guard) | **S** | Write the dead `prompt_versions` table; alert on changed-but-unbumped prompts. |

> **Decisions** are consolidated at the [bottom](#decisions-needed), each with a recommended default.
> **Important:** features 1 and 2 share edits — see [Cross-feature note](#cross-feature-note-do-1--2-together).

---

## 1. Feedback capture

**What/why.** Make the dormant `review_feedback` table get written so `feedback_stats()`
(`api/app/queries/metrics.py:244`) and `GET /api/v1/metrics/feedback` stop returning empty. Two signals:

- **Signal A — human (webhook, primary).** A new `pull_request_review_thread` handler: when a developer
  marks one of REVA's inline-finding threads **resolved** (accept) or **unresolved** (reject/reopen),
  map the thread's root comment → owning finding → write a `review_feedback` row
  (`is_positive = action == "resolved"`).
- **Signal B — auto-verified (worker, free).** In the delta loop, `is_resolved()` is already computed at
  `runner.py:751` and discarded; persist a `resolved` verdict as a positive row
  (`reaction="auto_verified"`). Zero extra Claude cost; a distinct `reaction` value so it never collides
  with Signal A on the unique key.

REST reaction polling and reply-sentiment classification are **out of scope** (speculative; see decisions).

**Files touched**

- `reva/db/writers.py` — **new** `record_feedback(db, *, review_finding_id, review_run_id,
  github_comment_id, reactor_login, reaction, is_positive)` near `lookup_finding_by_comment_id`; insert a
  `ReviewFeedback` row, swallow `IntegrityError` on `uq_review_feedback_unique` (idempotent), mirroring
  `record_github_event` (`writers.py:588`).
- `reva/db/writers.py` — **edit** `lookup_finding_by_comment_id` *and* `get_open_findings_for_pr` to also
  return `review_run_id` (both feed the NOT NULL FK `review_feedback.review_run_id`).
- `api/app/routes/webhooks.py` — **edit** add `elif event == "pull_request_review_thread":
  _handle_review_thread(db, payload)` to `_process_delivery` (after line 97) + the handler: early-return
  unless action ∈ {resolved, unresolved}; ignore Bot senders (anti-loop); find the **root** comment
  (`in_reply_to_id is None`) → `lookup_finding_by_comment_id` → `record_feedback`.
- `worker/worker/runner.py` — **edit** in `_verify_and_resolve_findings`, after `resolve_review_thread`
  (the `is_resolved` true branch, ~line 751), best-effort `record_feedback(... reaction="auto_verified",
  is_positive=True)`.
- `api/app/queries/metrics.py` — **no schema change**; `feedback_stats` already buckets by `is_positive`.

**Data model.** `review_feedback` (migration 002, ORM `models.py:292`). Dedup key is the existing
`uq_review_feedback_unique = (review_finding_id, reactor_login, reaction)` — **not** `github_comment_id`.
A resolve-then-unresolve writes two rows (different `reaction`); a re-resolve dedups. Signal B's
`reaction="auto_verified"` never collides with Signal A's `resolved`/`unresolved`. `is_positive=True` for
{`resolved`, `auto_verified`}, `False` for {`unresolved`}.

**Config/operator.** **No migration** if Signal B reuses the finding's own (non-null) `github_comment_id`
— recommended. **Operator must subscribe the GitHub App to the `Pull request review thread` event**
(Settings → Permissions & events → Subscribe to events). No new permission (uses `Pull requests: Read`).

> ⚠️ **Verification fixes.**
> 1. `lookup_finding_by_comment_id` (`writers.py:760`) and `get_open_findings_for_pr` (`writers.py:721`)
>    currently do **not** return `review_run_id` — both must be extended (existing callers ignore extra
>    dict keys, so it's backward-compatible).
> 2. **Signal B `reactor_login`:** there is no bot-login constant in the code (`AGENT_NAME="REVA"` is a
>    display string, not a login). Pick a sentinel — see [decision A].
> 3. **`thread.comments` shape** isn't fully enumerated in the docs; validate against a real delivery in
>    staging before shipping (the root-comment-id mapping depends on `in_reply_to_id is None`).

**Tests.** `api/tests/test_webhooks.py` (resolved→positive, unresolved→negative, non-REVA thread→nothing,
Bot sender ignored, duplicate delivery no-op, malformed payload→202); `api/tests/test_v1_metrics.py`
(real counts after a resolve); writers tests (insert, dedup, both lookups return `review_run_id`);
`worker/tests/test_runner.py` (auto-verified row written; a feedback-write failure doesn't abort the loop).

---

## 2. Per-finding outcome ledger

**What/why.** Persist the implicit ground truth REVA computes and discards. `_verify_and_resolve_findings`
(`runner.py:690`) already decides `is_resolved()` per finding on each delta re-review; record it as a
per-finding lifecycle. Two transitions:

- `resolved_by_fix` — set in the delta loop when `is_resolved()` is true (after `resolve_review_thread`).
- `still_open_at_merge` — set when the PR **merges** (`pull_request` action `closed` with `merged=true`)
  for findings still `open` that were actually posted (`github_comment_id IS NOT NULL`).

**Files touched**

- `db/migrations/015_finding_outcome.sql` — **new**: `ALTER TABLE review_findings ADD COLUMN IF NOT
  EXISTS outcome TEXT NOT NULL DEFAULT 'open'; ADD COLUMN IF NOT EXISTS outcome_at TIMESTAMPTZ;` +
  partial index `idx_findings_outcome ... WHERE outcome <> 'open'`.
- `reva/db/models.py` — **edit** mirror the two columns + the partial index (with `sqlite_where` so the
  test schema matches) on `ReviewFinding` (class at line 196).
- `reva/db/writers.py` — **new** `set_finding_outcome(db, finding_id, outcome)` and
  `mark_open_findings_at_merge(db, pull_request_id)` (UPDATE … WHERE `outcome='open'` AND
  `github_comment_id IS NOT NULL`; returns rowcount).
- `worker/worker/runner.py` — **edit** call `set_finding_outcome(..., "resolved_by_fix")` after
  `resolve_review_thread` in the `is_resolved` true branch (ordering matters — see edge cases).
- `api/app/routes/webhooks.py` — **edit** in `_handle_pull_request` (line 118), handle `action=="closed"`
  with `merged is True` → `mark_open_findings_at_merge`, **before** the `_REVIEWABLE_ACTIONS` early return.
- `db/migrations/README.md` — **edit** add the 015 row.

**Data model.** Column-on-table (not a side table): `outcome` is a single-valued 1:1 attribute, no history
needed (Simplicity-First). States: `open` (default, never written explicitly) → `resolved_by_fix`
(terminal) or `still_open_at_merge` (terminal). `resolved_by_fix` wins because the merge sweep only touches
`outcome='open'`. No new webhook subscription — `pull_request` (incl. `closed`) is already subscribed.

> ⚠️ **Verification note.** The plan's phrasing *"opened/synchronize payloads do not carry `merged`"* is
> slightly off — `merged` is always present, just `false` until a merge. Functionally fine (only read it on
> `action=closed`). **Ordering is load-bearing:** `set_finding_outcome` must run **after**
> `resolve_review_thread`, so a failed resolve doesn't mislabel a finding as fixed.

**Tests.** `worker/tests/test_runner.py` (resolved→`resolved_by_fix`; not-resolved→unchanged; resolve
raises→outcome stays `open`); `api/tests/test_webhooks.py` (closed+merged marks open posted findings;
closed+unmerged marks nothing; redelivery idempotent); writers tests (sets outcome+timestamp; only touches
open+posted; idempotent).

---

## 3. Prompt-version registry + drift guard

**What/why.** Write the dead `prompt_versions` table and detect unbumped prompt edits. On worker startup,
hash the prompt files REVA actually assembles and UPSERT one row per version string (from
`PromptBuilder.get_version()`, today `v1.5`). If the version string is unchanged but the hashes differ →
**drift**: log a warning and fire a Google Chat operational alert.

**Files touched**

- `reva/prompt_builder.py` — **edit** add `compute_prompt_hashes(self, skills_dir) -> (system_hash,
  review_hash)`: `system_hash = sha256(review_guidance.md)`; `review_hash = sha256(odoo19.md + every
  skills/*.md, sorted & filename-labeled)`. `import hashlib`.
- `reva/db/writers.py` — **new** `register_prompt_version(db, version, system_prompt_hash,
  review_prompt_hash, description=None) -> str` returning `created` / `unchanged` / `drift`, decorated
  `@_retry_on_conflict` (mirrors `upsert_repository`, `writers.py:399`). On drift, leave the stored row
  untouched (first-seen hashes are the baseline). Add `PromptVersion` to the model import.
- `worker/worker/runner.py` — **edit** in `build_worker_context()` (line 116), after `db.migrate()` and
  `PromptBuilder(...)`, call a best-effort `_register_prompt_version(...)`; on `drift` →
  `logger.warning("prompt_drift_detected")` + `notify_operational_alert(...)`.

**Config/schema.** **No migration** — `prompt_versions` already exists (003) and the ORM mirrors it
(`models.py:522`). No new env vars (`GOOGLE_CHAT_WEBHOOK_URL`, `REVA_SKILLS_DIR` exist).

> ⚠️ **Verification fixes.**
> 1. **Column semantics (document it):** the CLI review path **never reads `system.md`** — it assembles
>    `review_guidance.md` (+ `odoo19.md` if odoo) + the skill. So reuse the legacy columns with new
>    meaning: `system_prompt_hash` ← `review_guidance.md`. State this clearly in the writer docstring; the
>    column name is inherited from the old Messages-API split.
> 2. **`notify_operational_alert` import:** `runner.py:30` imports only `notify_worker_error, post_to_chat`
>    — add `notify_operational_alert`.
> 3. **Missing-file handling:** let `FileNotFoundError` propagate from `compute_prompt_hashes` to the
>    startup try/except (skip registration) rather than hashing an empty string and recording a misleading
>    hash. (`_build_preamble` swallows it at review time — the hasher must not.)

**Tests.** `worker/tests/test_prompt_files.py` (hashes are deterministic 64-hex; changing
`review_guidance.md` flips only `system_hash`, changing a skill flips only `review_hash`; order-independent);
`worker/tests/test_db.py` (`created` / `unchanged` / `drift` branches; a new version string creates a second
row); `worker/tests/test_runner.py` (drift path emits the alert; `get_version` ValueError is swallowed at
boot; clean DB registers without alerting).

---

## Cross-feature note: do 1 & 2 together

Features **1 (Signal B)** and **2** both wire into the *same* spot — the `is_resolved` true branch at
`runner.py:751` — and both require adding `review_run_id` to `get_open_findings_for_pr`. Implement them in
one pass (or back-to-back) to avoid editing the delta loop and that writer twice.

They also **overlap semantically**: a REVA-verified fix would be recorded *both* as a `review_feedback`
auto-verified row (feature 1, Signal B) *and* as `outcome='resolved_by_fix'` (feature 2). That's the same
event in two tables. See [decision E] — I lean toward **dropping Signal B** and letting the outcome ledger
own "REVA verified a fix," keeping `review_feedback` for genuine *developer* signal (thread resolution).
That makes `approval_rate` mean "developers acted on it," uncontaminated by REVA's self-assessment.

---

## Decisions needed

| # | Decision | Recommended default |
|---|---|---|
| A | **Signal B `reactor_login` value** — no bot-login constant exists. | A module constant `"reva[bot]"` (or a `Settings.bot_login`); it's a sentinel, so exact value is cosmetic. |
| B | **Feedback-capture migration** — reuse the finding's own `github_comment_id` for Signal B (no migration) vs relax `NOT NULL`. | **Reuse** (no migration; every delta candidate already has a non-null comment id). |
| C | **Review-body copy** — keep *"React with 👍/👎"* (uncapturable) or change to *"Resolve the comment thread once you've addressed it"*. | **Change it** — don't promise a signal we can't capture. |
| D | **`feedback_stats` segmentation** — lump auto-verified with human signal, or split. | **Lump** for Tier 1 (split is a follow-up). *(Moot if E drops Signal B.)* |
| E | **Signal B vs outcome ledger overlap** — record REVA-verified fixes in `review_feedback` too, or only in the outcome ledger. | **Only the outcome ledger** — keep `review_feedback` = developer signal. Drops Signal B; simplest, cleanest semantics. |
| F | **Outcome states** — is `resolved_by_fix` terminal? add a `closed_unmerged` state? | `resolved_by_fix` **terminal**; **no** extra state (leave abandoned-PR findings `open`). |
| G | **Prompt-hash skill set** — hash all `skills/*.md` (incl. README) or only the four `reva-*.md`. | **All `*.md` sorted** — catches any drift; document the choice. |
| H | **Drift action** — warn + Chat alert only, or also refuse-to-start / open an issue. | **Warn + alert** only (the eval-gate is a later tier). |

## Suggested order

1. **Prompt-version registry** (3) — fully independent, S, no migration, no webhook. Lowest risk; land first.
2. **Outcome ledger** (2) — S; one migration; owns "REVA verified a fix" if decision E holds.
3. **Feedback capture** (1) — M; the `pull_request_review_thread` handler + the developer-resolution signal.
   Do its `get_open_findings_for_pr`/delta-loop edits **together with** feature 2's.

Each ships as its own commit + green `make test` (worker/api/scheduler) + `ruff check`. Feature 1 also needs
the operator to enable the `Pull request review thread` webhook event before it does anything live.
