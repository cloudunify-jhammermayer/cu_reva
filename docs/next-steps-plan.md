# Next steps plan (2026-06-05)

The functional surface is complete (reviews diff/diff-all/full/deep, comment
replies, delta resolution, repo audits, TUI, prod stack behind a Cloudflare
tunnel) and CI is green — but **almost none of it has run in production yet**.
This plan is prioritized by what blocks a trustworthy product, not by effort.

Each item: **what / why it matters / approach / touches / test / size.**

---

## P0 — fix before trusting it day-to-day

### P0-1 · Comment commands on unknown PRs
- **What:** `/review`, `/review-all`, `/full-review`, `/deep-review` only act on PRs REVA already has a row for. A PR opened before the app was installed logs `comment_trigger_pr_not_found` and is silently ignored.
- **Why:** high everyday friction — the most common "why did nothing happen?" report.
- **Approach:** in `_handle_issue_comment`, on a DB miss fetch the PR from the GitHub API (handler already has a `github` client + installation id), upsert it (`writers.upsert_pull_request`), then proceed down the normal enqueue path. Reuse the same association/auth gate as a known PR.
- **Touches:** `api/app/routes/webhooks.py`; possibly a `get_pull_request` on `reva/github_client.py`; `reva/db/writers.py`.
- **Test:** webhook test — comment command on an unknown PR → fetch+upsert + `pending_review_upserted` (mock the GitHub fetch). Keep the existing "not found → ignored" test only for the genuine 404 case.
- **Size:** S–M.

### P0-2 · Push silently downgrades a queued deep/full review to diff (CORR-7)
- **What:** `pending_reviews` is unique on `(repository_id, pr_number)`. If `/deep-review` is queued and a push (`synchronize`, default `diff`) lands during the debounce window, the upsert overwrites `review_mode` → the deep review silently becomes a diff review.
- **Why:** users explicitly ask for deep and silently get diff; erodes trust.
- **Approach:** on upsert, don't downgrade an unconsumed pending row's mode — keep the higher-intent mode (precedence `deep > full > diff-all > diff`), or only overwrite `head_sha`/`scheduled_at` while preserving the stronger mode. Decide precedence explicitly.
- **Touches:** `api/app/routes/webhooks.py` (upsert path) + `reva/db/writers.py` (`upsert_pending_review`).
- **Test:** queue deep → simulate a `synchronize` diff event for the same PR → assert the pending row stays `deep` (new SHA, new schedule).
- **Size:** S.

---

## P1 — soon (correctness + safety, not blocking first deploy)

### P1-1 · Finish review-comment pagination (CORR-8 remainder)
- **What:** `get_review_comments` was paginated (2026-06-05); `get_review_threads` still fetches one page. On a busy PR, resolution/reply matching misses threads past page 1.
- **Approach:** paginate `get_review_threads` (GraphQL `after`/`hasNextPage`, or REST per_page+page) the same way.
- **Touches:** `reva/github_client.py`; test in `worker/tests/test_github_client.py`.
- **Size:** S.

### P1-2 · Make context-line findings resolvable (inline mapping)
- **What:** `split_findings` only makes a finding inline when its `line_start` is inside a diff hunk. Findings about existing/untouched code (common in Odoo logic) land in the review **body**, so they can't be tracked or auto-resolved (the Aurium #60 reality).
- **Approach (pick one, brainstorm first):** (a) snap a finding to the nearest changed line in the same file so it becomes an inline thread; or (b) accept body findings as non-resolvable and document it; or (c) post body findings as a single file-level comment. (a) maximizes resolvability but risks mis-anchoring — needs care.
- **Touches:** `reva/review_formatter.py` (`split_findings`), tests.
- **Size:** M (design decision first).

### P1-3 · `developer_stats` join fan-out (CORR-3)
- **What:** `review_count` / `avg_findings` are inflated because `count(ReviewRun.id)` counts the `ReviewFinding` outer-join fan-out; the leaderboard sort is wrong.
- **Approach:** `count(distinct ReviewRun.id)`; compute avg findings without the finding join (subquery or separate agg).
- **Touches:** `api/app/queries/metrics.py`; test the corrected counts.
- **Size:** S.

### P1-4 · Close the highest-risk test gaps
- **TEST-2:** API bearer-token auth is untested (fixtures set no key → auth effectively open in tests). Add positive/negative auth tests across the v1 sub-routers.
- **TEST-3:** the Go TUI has zero tests — add httptest-based client tests (auth header, error propagation, query escaping) + a `truncate` UTF-8 test.
- **Size:** M.

---

## P2 — scale-out / later (mostly bounded or deferred by decision)

- **D1/D2 integration tests (testcontainers):** real-Postgres coverage for SKIP-LOCKED / advisory-lock paths (a partial `integration (postgres)` tier exists; broaden it).
- **A2 egress lock:** validate the proxy overlay in staging (`docs/egress-lockdown.md`) before enforcing in prod.
- **INFR-3:** graduate `mypy` + `golangci-lint` from advisory to blocking after one verified clean run. (gitleaks + semgrep are already blocking.)
- **deploy.sh (INFR-8/9/10):** make it zero-downtime, give the health check a real pass/fail gate, and guard the `git pull` with a clean/pinned ref.
- **INFR-12:** over-budget decline should raise an operator alert, not just a log + PR comment.
- **SECU-24:** add a root `.dockerignore` (keeps `.venv`/`secrets/`/`.env` out of the build context).
- **Backups:** restore drill at the current schema (R10) once past testing.
- **Scheduled audits:** audits are **manual-only** today (API/TUI trigger). Decide whether to add a periodic (cron) audit per repo — deliberate product call, not a bug.
- **CONC-\* set:** mostly bounded and gated to the N-replica milestone (CONC-3 retry re-pay, CONC-6 debounce re-check, CONC-7 in-flight budget, CONC-9 SIGKILL requeue).
- **CI action versions (INFR-14):** pin GitHub Actions + the gitleaks image by SHA; finish the Node-24 migration (interim: `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24` is set workflow-level).

---

## Pre-deploy live-test checklist

Run these on the deployed server (tunnel up) — they exercise paths no unit test covers:

1. **Tunnel + webhook path** — open a PR on an installed repo; confirm `pending_review_upserted` in api logs and a Check Run appears. (Proves the tunnel + `CF-Connecting-IP` allowlist actually pass GitHub's hooks.)
2. **CodeGraph is *used*** — `/deep-review` on a real PR; confirm `codegraph_index_ready`, **and** that the model actually calls `mcp__codegraph__*` (not just that the server started). Steering is mandatory — verify it took.
3. **Full resolution cycle** — review posts inline comments → `finding_comment_ids_backfilled count=N`; push a fix → re-review → `delta_resolution_done {resolved>0}` and the thread resolves on GitHub.
4. **Audit end-to-end** — trigger via TUI Repos `a` or `POST /repos/{id}/audit`; confirm `audit_job_done`, `reva-audit` label created, issues opened for major/critical, and findings visible via `GET /api/v1/audit-findings` + TUI Audits tab.
5. **Auth** — TUI against prod with the correct `REVA_API_KEY` works; a wrong/missing key returns 401.
6. **Comment reply** — reply to one of REVA's inline comments; confirm a follow-up answer is posted and it never replies to a bot.
7. **404** — hit `https://reva.dev.cloudunify.org/` → the branded cloud 404.
8. **Budget cap (optional)** — set a low `REVA_DAILY_BUDGET_USD`, confirm a new review/audit is declined (not run) once the cap is hit.
