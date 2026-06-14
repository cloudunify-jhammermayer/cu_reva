# Tier 0 + Tier 1 — Implementation & Testing Guide

What shipped in this work session (roadmap Tiers 0 and 1), how to test each piece, and what to
expect in the real app. Companion to the plans in [`docs/tier0-plan.md`](tier0-plan.md) and
[`docs/tier1-plan.md`](tier1-plan.md) and the [`FEATURE_ROADMAP.md`](../FEATURE_ROADMAP.md).

## What shipped (commits)

| Commit | Change |
|---|---|
| `080f731` | Tier 0 — per-repo Check Run severity gating (`block_on_severity`) |
| `f8d311e` | Tier 0 — deterministic Odoo severity calibration |
| `a5257bc` | Tier 0 — `reva-risk-*` PR labels |
| `0d27ed2` | Tier 0 — trivial-diff short-circuit (`skipped_trivial`) |
| `3dfe5b0` | fix — trivial-diff must not skip a statement reorder |
| `bb752c5` | Tier 1 — prompt-version registry + drift guard |
| `ac8c54b` | Tier 1 — per-finding outcome ledger |
| `dbb174e` | Tier 1 — feedback capture via `pull_request_review_thread` |

## Running the tests

Per-service venvs, Python 3.14, each installs `reva/` editable. From the repo root:

```bash
# one-time per service (if .venv is missing or stale — e.g. ModuleNotFoundError: defusedxml):
cd worker && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # same for api/, scheduler/

# run a service's suite
worker/.venv/bin/python    -m pytest worker/tests/    -q     # 506 passed, 6 skipped
api/.venv/bin/python        -m pytest api/tests/        -q     # 147 passed
scheduler/.venv/bin/python  -m pytest scheduler/tests/  -q     # 28 passed, 1 skipped

# all three at once (uses the existing .venvs)
make test

# lint (ruff is blocking in CI; install once into a venv if not present)
worker/.venv/bin/python -m ruff check reva worker/worker api/app scheduler/scheduler
```

Unit tests use SQLite in-memory + `httpx` MockTransport + subprocess-mocked CLI — no Docker, no
network. **Concurrency and the raw migration SQL are NOT exercised by the unit suite** (SQLite builds
the schema from the ORM via `create_all`, not from `db/migrations/*.sql`). Migration 015's SQL and the
`FOR UPDATE`/advisory-lock paths are only covered by `make test-integration` (throwaway Postgres,
needs Docker).

---

## Tier 0

### 1. Per-repo severity gating (`block_on_severity`)

**What:** the Check Run conclusion's blocking threshold is configurable per repo. Default `major`
preserves the old behavior (any major/critical → red `failure`).

**Configure** in a repo's `.claude-review.yml`:
```yaml
block_on_severity: critical   # one of: critical | major | minor | none   (default: major)
```
- `critical` — only critical findings fail the check; major/minor → non-blocking `neutral`.
- `none` — never fails (advisory mode).
- An invalid value (typo) falls back to `major` and is logged, not fatal.

**Test (unit):** `worker/tests/test_review_formatter.py` (`test_gate_*`), `test_reviewer.py`
(`test_block_on_severity_*`), `test_runner.py` (`test_block_on_severity_critical_makes_major_non_blocking`).

**Expect on a PR:** with `block_on_severity: critical`, a PR whose worst finding is `major` shows a
**neutral** (not failing) REVA check; with the default, the same PR shows **failure**.

### 2. Deterministic Odoo severity calibration

**What:** findings flagged `is_odoo_specific` get their severity **floored** to the minimum documented
in `prompts/odoo19.md` for canonical anti-patterns (raw `cr.execute` w/ string formatting → critical;
`cr.commit`/`cr.rollback`, missing `ir.model.access.csv`, `sudo()` in controllers, `auth='none'`,
`@api.depends` gaps, `@api.onchange` DB writes, CSP inline scripts, missing manifest deps → major). It
never downgrades a model-raised-higher severity; an injection-cue guard avoids false-flooring a safe
parameterized `cr.execute`.

**Test (unit):** `worker/tests/test_reviewer.py` (`test_calibrate_*`,
`test_execute_calibration_lifts_risk_level`); a sync-guard `test_prompt_files.py::test_odoo_severity_rules_in_sync_with_odoo19_md` fails if the rule table and `odoo19.md` drift apart.

**Expect on a PR:** an Odoo repo (`odoo: true`) PR where Claude reports a raw `cr.execute` string-format
finding as `minor` now lands as **critical**, and the risk level / Check Run reflect that. Non-Odoo
findings are untouched. (Note: the repo-audit path is **not** calibrated — a known, intentional gap.)

### 3. Risk labels (`reva-risk-*`)

**What:** after a completed review, REVA sets one mutually-exclusive label
`reva-risk-{critical|high|medium|low}` on the PR (removing any prior `reva-risk-*` it set), so leads can
triage from the PR list. Best-effort — a label-API failure is logged and never fails the review.

**Test (unit):** `worker/tests/test_github_client.py` (`test_get_issue_labels_*`, `test_add_labels_*`,
`test_remove_label_*`), `test_runner.py` (`test_completed_run_sets_risk_label`,
`test_re_review_downgrade_removes_prior_risk_label`, `test_same_risk_level_is_noop`,
`test_label_failure_does_not_fail_review`, `test_declined_review_sets_no_risk_label`).

**Expect on a PR:** a `reva-risk-high` (etc.) label appears after review. On a re-review that lowers
risk, the old label is removed and the new one added. Declined/stale reviews get no label. Needs the
GitHub App's existing `Pull requests: write` (used for issue comments) — **no new permission**.

### 4. Trivial-diff short-circuit + reorder fix

**What:** if the entire reviewable diff is only whitespace, comment-only, or import-reordering changes,
REVA skips the paid Claude call entirely and posts a non-blocking `skipped` Check Run (new
`skipped_trivial` status, zero findings, zero cost). **Conservative by construction:** any real
added/removed line, a deleted/renamed/binary file, or — after the `3dfe5b0` fix — a *reordering* of
identical lines makes the diff non-trivial and gets a normal review.

**Test (unit):** `worker/tests/test_diff_utils.py` (`test_trivial_*`, `test_nontrivial_*` incl.
`test_nontrivial_statement_reorder`), `test_reviewer.py` (`test_trivial_diff_skips_without_calling_claude`),
`test_runner.py` (`test_skipped_trivial_posts_only_skipped_check`).

**Expect on a PR:** a push that only reindents or edits comments produces a green/neutral "skipped — no
substantive changes" check with **no Claude spend** and no inline comments. A push that reorders two
statements is reviewed normally (the safety fix).

---

## Tier 1 — the feedback loop

> Tier 1 activates infrastructure REVA already had but never wrote to. Most of it is invisible until
> data accumulates (it powers `GET /api/v1/metrics/feedback` and the future quality dashboard).

### 5. Prompt-version registry + drift guard

**What:** on worker startup, REVA hashes the prompt files the CLI actually assembles
(`review_guidance.md` → `system_prompt_hash`; `odoo19.md` + all `skills/*.md` → `review_prompt_hash`)
and upserts a `prompt_versions` row keyed by the CHANGELOG version (`v1.5`). If the version string is
unchanged but a hash differs → **drift**: a warning log + a Google Chat operational alert.

**Test (unit):** `worker/tests/test_prompt_files.py` (`test_compute_prompt_hashes_*`,
`test_changing_review_guidance_flips_only_system_hash`, `test_changing_a_skill_flips_only_review_hash`),
`test_db.py` (`test_register_prompt_version_*`), `test_runner.py` (`test_register_prompt_version_*`).

**Expect:** first boot inserts `v1.5`. Edit a prompt file **without** bumping the `##` heading in
`prompts/CHANGELOG.md`, restart the worker → `prompt_drift_detected` log + (if
`GOOGLE_CHAT_WEBHOOK_URL` is set) a "Prompt drift detected" Chat alert. Bumping the CHANGELOG version
clears it (new row). Drift re-alerts on each restart until the version is bumped — intended nag.

### 6. Per-finding outcome ledger

**What:** persists the fix-verdict REVA already computes in the delta-resolution loop. `review_findings`
gains `outcome` (`open` → `resolved_by_fix` | `still_open_at_merge`) + `outcome_at`. `resolved_by_fix`
is set when the verifier confirms a fix on a later push (after the thread is resolved). On a **merged**
PR, every still-open *posted* finding is marked `still_open_at_merge`. Zero extra Claude cost.

**Migration:** `db/migrations/015_finding_outcome.sql` (runs at startup on Postgres; the ORM mirror
gives the SQLite test schema the same columns + partial index).

**Test (unit):** `worker/tests/test_db.py` (`test_set_finding_outcome_*`,
`test_mark_open_findings_at_merge_*`, `test_findings_default_to_open_outcome`), `test_runner.py`
(`test_verify_and_resolve_*` — sets `resolved_by_fix` after resolve, never if not resolved or if resolve
raises), `api/tests/test_webhooks.py` (`test_pr_closed_merged_marks_open_findings`,
`test_pr_closed_unmerged_marks_nothing`).

**Expect:** query the DB — after a developer fixes a flagged issue and pushes, that finding's row shows
`outcome='resolved_by_fix'`; after a PR merges with unfixed findings, those show
`still_open_at_merge`. No PR-visible change yet (this is data for analytics/dashboards). Only merges
count — an abandoned (closed-unmerged) PR leaves findings `open`.

### 7. Feedback capture (`pull_request_review_thread`)

**What:** writes the dormant `review_feedback` table. **GitHub fires no webhook for 👍/👎 reactions**, so
the captured signal is **thread resolution**: when a developer marks one of REVA's finding comment
threads **resolved** (accept) or **unresolved** (reject), REVA records a `review_feedback` row
(`is_positive = resolved`). Anti-loop (ignores Bot senders), idempotent (unique-constraint dedup). The
review-body footer now asks devs to *resolve the thread once addressed* (the capturable signal).

> **Decision applied:** REVA-verified fixes are recorded only in the outcome ledger (#6), **not** also
> as feedback — so `review_feedback` stays pure *developer* signal and `approval_rate` isn't polluted by
> REVA's self-assessment.

**Test (unit):** `api/tests/test_webhooks.py` (`test_review_thread_resolved_writes_positive_feedback`,
`..._unresolved_writes_negative_feedback`, `..._non_reva_thread_writes_nothing`, `..._by_bot_is_ignored`,
`..._resolve_twice_deduped`, `..._malformed_payload_accepted`), `worker/tests/test_db.py`
(`test_record_feedback_*`, `test_lookup_finding_by_comment_id_returns_review_run_id`).

> ⚠️ **Operator action required — feedback capture is dormant until you do this.** In the GitHub App
> settings (**Permissions & events → Subscribe to events**), enable **`Pull request review thread`**.
> No new permission is needed (it uses `Pull requests: Read`). Until enabled, no
> `pull_request_review_thread` deliveries arrive and `review_feedback` stays empty.

**Expect once enabled:** resolve a REVA inline-comment thread on a PR → a `review_feedback` row appears
and `GET /api/v1/metrics/feedback` starts returning non-null `thumbs_up`/`approval_rate` for that
category. Resolving a non-REVA thread, or a bot resolving one, writes nothing.

---

## Live / end-to-end checklist

Unit tests don't exercise the real GitHub CLI or webhooks. To validate on a staging install:

1. **Severity gating:** add `block_on_severity: critical` to a test repo's `.claude-review.yml`; open a
   PR with a major-but-not-critical issue → REVA check is `neutral`, not failing.
2. **Risk label:** open a PR → confirm a `reva-risk-*` label appears; push a fix that lowers risk →
   label swaps.
3. **Trivial skip:** push a whitespace-only commit → "skipped — no substantive changes" check, no
   spend (confirm via the worker log `review_skipped_trivial` and zero `claude_spend` row).
4. **Prompt drift:** edit `prompts/odoo19.md` without bumping `CHANGELOG.md`, restart worker → drift
   alert.
5. **Outcome ledger:** flag → fix → push; confirm `review_findings.outcome='resolved_by_fix'`. Merge a
   PR with an open finding; confirm `still_open_at_merge`.
6. **Feedback capture:** enable the webhook event; resolve a REVA thread → `review_feedback` row +
   `/api/v1/metrics/feedback` shows data.

## Migrations & DB

- **015_finding_outcome.sql** is the only new migration; applied idempotently at startup by
  `Database.migrate()`. Run `make test-integration` (Docker) to exercise it against real Postgres
  before deploying.
- **No migration** for prompt-version or feedback capture — both reuse tables created long ago
  (`prompt_versions` / `003`, `review_feedback` / `002`) that were never written to until now.
