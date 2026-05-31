# REVA — Production-Readiness Remediation Plan

**Status:** ✅ implemented (R1–R16) on branch `feature/production-readiness`. Tests-first; suites green (worker 241, api 68, scheduler 16, Go build/vet/test). mypy + golangci-lint added to CI as *advisory* (not yet gating) per R9; ruff is a blocking gate. R10's backup script is syntax-validated — a live backup+restore still needs a one-time verification on the host.
**Date:** 2026-05-31
**Lead lens:** reliability / crash-safety. **Topology assumption:** single instance today, scaling to multiple workers/schedulers soon — so concurrency/idempotency items are treated as first-class, not deferred.

Effort key: **S** ≈ <½ day · **M** ≈ ½–2 days · **L** ≈ >2 days.
Every item lists a *success criterion* — the verifiable check that closes it (tests-first where practical).

This plan only contains findings verified against the code. Three over-reported subagent "criticals" (repo-cache path traversal, "202-then-data-loss" webhook, subprocess API-key leak) were checked and rejected; they are not listed.

---

## P0 — Crash-safety & correctness (lose reviews or spend in production today)

### R1 · Align RQ job timeout with the CLI subprocess timeout — **S**
- **Problem:** `_JOB_TIMEOUT = 900`s (`scheduler/scheduler/poller.py:25`) is shorter than the Claude CLI subprocess timeout `_SUBPROCESS_TIMEOUT = 1500`s (`reva/claude_code_runner.py:30`). RQ SIGKILLs the work-horse at 15 min while the code believes it has 25 — deep/Opus reviews on large repos are killed mid-run, burning spend with no result and a misleading failure.
- **Fix:** derive both from one source of truth (e.g. job timeout = subprocess timeout + a fixed overhead buffer for git + posting). Pick the real ceiling deliberately (proposal: subprocess 1500s, job 1800s).
- **Impact:** High — eliminates silent loss of the most expensive reviews.
- **Success criterion:** a test asserting `job_timeout > subprocess_timeout + buffer`; a deep review that runs >15 min completes and posts.

### R2 · Bound git clone/fetch with a timeout — **S**
- **Problem:** `_run_git` calls `subprocess.run(["git", ...])` with **no `timeout`** (`reva/claude_code_runner.py:259`). Clone/fetch runs while the per-repo `flock` is held (`:75`), so a hung network op blocks *every* job for that repo until a container restart.
- **Fix:** add a timeout to git ops (clone longer than fetch), and map `subprocess.TimeoutExpired` → `TransientError` so RQ retries.
- **Impact:** High — removes an unrecoverable head-of-line stall.
- **Success criterion:** unit test — mocked `TimeoutExpired` raises `TransientError`; no git call lacks a timeout.

### R3 · Recover reviews left `running` after a forced kill / crash — **M**
- **Problem:** `record_review_started` writes `status="running"` (`reva/db/writers.py:65-72`). RQ *does* warm-shutdown on SIGTERM, but Docker's default ~10s stop grace can't cover a multi-minute review → SIGKILL mid-job → row stuck `running` forever. No reaper exists.
- **Fix:** (a) set `stop_grace_period` in compose to exceed the job timeout (or document a drain step); (b) add a sweep (scheduler tick or worker startup) that moves `running` rows older than `2 × job_timeout` to `failed` (optionally requeue), with a notification.
- **Impact:** High — prevents orphaned rows that corrupt metrics and hide failures; clean rolling deploys.
- **Success criterion:** test that a `running` row older than the threshold is reaped to `failed`; compose `stop_grace_period` ≥ job timeout.

### R4 · Make GitHub posting idempotent across the create→persist window — **M**
- **Problem:** `runner.py:287-290` posts the PR review, then calls `attach_github_ids`. A crash *between* those two lines means the GitHub ID is never stored; on retry, `get_posted_github_ids` (`:284`) sees nothing and posts a **duplicate** review. (Window is narrow thanks to `review_runs UNIQUE(repo,pr,sha,mode)`, but real.)
- **Fix:** before creating, detect an existing REVA review/check on the PR via the GitHub API (list reviews/check-runs for the head SHA by the app) and reuse it; or persist an intent record before the API call. Apply the same guard to the check-run post (`:292-304`).
- **Impact:** Medium-High — avoids duplicate reviews developers see on retried jobs.
- **Success criterion:** test simulating crash-after-post-before-attach → retry posts **zero** new GitHub objects.

### R5 · Close the webhook partial-failure gap that silently drops reviews — **M**
- **Problem:** `_process_delivery` records the event first (`webhooks.py:68-76`); if a later writer (`upsert_pending_review`, `:130`) fails, the request 500s and GitHub retries — but `record_github_event` now returns `None` ("duplicate"), so the retry returns early (`:77-79`) and the review is **never queued**.
- **Fix:** either wrap event-record + downstream upserts in one transaction (atomic), or split "received" from "processed" so the idempotency gate only short-circuits fully-processed deliveries. Downstream writers are already upserts, so re-processing is safe.
- **Impact:** High — a transient DB blip currently means a lost review with no error surfaced.
- **Success criterion:** test — failure during `upsert_pending_review` then redelivery results in a pending review existing.

---

## P1 — Scale-readiness & fail-closed (do before running a 2nd worker/scheduler)

### R6 · Lock pending-review claiming for multiple schedulers — **M**
- **Problem:** the poller reads due IDs then re-checks `consumed` per row with no row lock (`scheduler/scheduler/poller.py:50-98`). Safe for one instance; with two schedulers, both can pass the check and double-enqueue. The `already_exists` guard (`:78-88`) covers auto reviews but **not** `manual_requeue`/`comment`.
- **Fix:** `SELECT … FOR UPDATE SKIP LOCKED` on the claim, and/or a unique guard on the enqueue key so duplicates can't materialize.
- **Impact:** High once scaled — prevents double reviews and double spend.
- **Success criterion:** concurrent-poller test enqueues each due review exactly once.

### R7 · Make the daily-budget check atomic — **M**
- **Problem:** `_budget_decline_if_exceeded` reads trailing spend then decides (`runner.py:214-217`) — non-atomic. N concurrent workers can each pass under the cap and collectively overshoot by up to N × per-review cost.
- **Fix:** reserve/charge under a row or advisory lock, or accept a documented overshoot bound ≤ one in-flight review per worker. Decide explicitly.
- **Impact:** Medium — bounds worst-case overspend when workers scale out.
- **Success criterion:** concurrent test shows total spend ≤ budget + one review.

### R8 · Make API-key auth fail closed — **S**
- **Problem:** `require_api_key` is a **no-op when the key is empty** (`api/app/dependencies.py:31-32`). Prod is protected only by a separate `REVA_REQUIRE_API_KEY` startup check; the dependency itself fails open, and the `require_api_key` setting isn't consulted by the dependency.
- **Fix:** have the dependency consult the enforcement flag and reject when auth is required but unconfigured (or always require outside an explicit dev mode). Keep the startup check as belt-and-suspenders.
- **Impact:** Medium — removes a misconfiguration footgun that exposes all `/api/v1/*`.
- **Success criterion:** test — required+unset key → 401 (not open).

---

## P2 — Quality gates & deployability (sustain quality, deploy safely)

### R9 · Add lint / type-check / coverage gates to CI — **M**
- **Problem:** CI runs only pytest + `go build/vet/test` (`.github/workflows/`). No `ruff`, `mypy`, `golangci-lint`, or coverage; regressions land silently.
- **Fix:** add `ruff check` + `mypy` for the three Python services and `golangci-lint` for the TUI; report coverage (gate later once a baseline exists).
- **Impact:** Medium — sustained quality, cheap to add.
- **Success criterion:** CI fails on a deliberately introduced lint/type error; green on `main`.

### R10 · Implement DB backups — **M**
- **Problem:** daily `pg_dump` is described in `doc/01-architecture.md` but **not implemented** (no script/cron). Host loss = data loss.
- **Fix:** `scripts/backup.sh` (pg_dump → off-host/object store), scheduled; document restore + a tested restore run.
- **Impact:** High for durability — currently no recovery path.
- **Success criterion:** a backup artifact is produced and a restore is verified once.

### R11 · Harden prod compose — **S**
- **Problem:** `docker-compose.prod.yml` lacks healthchecks/resource limits on api/scheduler/nginx (only worker/postgres have some), and base images are pinned to minor not patch.
- **Fix:** add healthchecks (`/health`) + CPU/mem limits to remaining services; pin image patch versions.
- **Impact:** Medium — predictable deploys, blast-radius containment.
- **Success criterion:** every service has a healthcheck + limits; images patch-pinned.

### R12 · Make `/health` check Redis, not just Postgres — **S**
- **Problem:** `/health` only runs `SELECT 1` (`api/app/routes/health.py`); a down Redis (the job broker) still reports healthy.
- **Fix:** add a Redis `ping()` to readiness.
- **Impact:** Medium — failed broker becomes visible to orchestration/alerts.
- **Success criterion:** test — Redis down → health reports unhealthy.

---

## P3 — Hardening (low risk at current threat model; batch when convenient)

- **R13 · SSRF allowlist** on Google Chat (`reva/notifications.py`) and Odoo callback (`reva/odoo_client.py`) URLs — operator-configured, so low risk; add scheme/host allowlist + reject private IPs. **S**
- **R14 · Cap pagination `offset`** (currently only `limit` is capped, e.g. `api/app/routes/v1/reviews.py`) to avoid deep-offset scans. **S**
- **R15 · Internal-API rate limiting** (behind auth + internal net today; matters more when exposed). **S**
- **R16 · Validate `owner`/`name` before path join** in `claude_code_runner.py:96` — defense-in-depth only (GitHub-signed, GitHub-constrained). **S**
- *Explicitly skipped:* webhook "replay window" — HMAC + `delivery_id` idempotency is the standard GitHub posture; not a gap.

---

## Suggested sequencing

1. **P0 (R1–R5)** — one PR per item, tests first, surgical. This is the bulk of crash-safety value.
2. **P1 (R6–R8)** — before provisioning a second worker/scheduler.
3. **P2 (R9–R12)** — R9 + R12 are quick wins; R10 (backups) is the highest-durability item here.
4. **P3** — batch opportunistically.

Open question for R7: is a bounded overshoot (≤ one in-flight review per worker) acceptable, or must the cap be hard? That choice sets S vs M effort.
