 # REVA — Production-Readiness Plan, Phase 2

**Status:** proposed (for review — no code yet). Follows Phase 1 (R1–R16, branch `feature/production-readiness`).
**Date:** 2026-05-31
**Scope:** the gap-analysis points beyond Phase 1's reliability/security hardening, *excluding* what we've explicitly dropped or deferred.

Effort key: **S** ≈ <½ day · **M** ≈ ½–2 days · **L** ≈ >2 days. Each item, once approved, is implemented tests-first (the R1–R16 way).

**Tooling cost & keys:** Sections **A, C, D, E, F need no new subscriptions or API keys** — they're code/config changes or free CLIs in CI. **Section C is $0 on the existing GitHub Team plan** (Dependabot is included; the rest run free in GitHub Actions — we deliberately avoid CodeQL/secret-scanning, which are paid Team add-ons ≈ $30/$19 per active committer/month). The only paid-vs-free / external-account decision is **Section B (observability)**: self-host (GlitchTip, Prometheus+Grafana, Tempo — no keys, code/diff snippets stay internal) vs SaaS (Sentry, Grafana Cloud — account + token, and code can leave your infra). Recommendation: **self-host**.

**Out of scope (decided in discussion):**
- **Review-quality eval loop + 👍/👎 capture** — deferred. The capture loop (GitHub-reaction polling → `review_feedback`) and the later golden-set/eval work are real effort to productionize; revisit once the core is settled. Server-side aggregation (`feedback_stats`, `/api/v1/metrics/feedback`) already exists and waits for data.
- **CD pipeline** — handled by odoo.sh.
- **Database backups** — deferred until past the testing phase (Phase-1 `scripts/backup.sh` sits ready).
- **Committable suggested-fixes / one-click apply** — rejected: it undercuts how juniors learn. REVA explains the *why*, not the patch.
- **Change "walkthrough" summaries** — REVA already posts a review `summary`; the extra neutral per-file walkthrough isn't worth it for this audience.

Ranked by my recommended order. **A** and **B1** are the highest-leverage.

---

## A — Prompt-injection hardening  *(highest priority)*

**Why (whole section):** REVA feeds attacker-controllable text (PR title, body, diff, and now the *cloned repo's own files/comments*) into a headless `claude` CLI running with `--dangerously-skip-permissions`. An April-2026 disclosure showed exactly this surface — a crafted PR title — hijacking Claude Code's security-review agent, Gemini CLI Action, and Copilot's coding agent. Injection can't be fully *prevented* at the model layer, so the accepted practice is defense-in-depth: shrink what the agent can *do* and what it can *reach*. REVA already does several things right (token-less clone, env allowlist, no host secrets in the subprocess, XML-delimited params) — these close the remaining holes.

### A1 — Drop `--dangerously-skip-permissions`; write output inside the clone — **S**  ✅ *done & verified against the live CLI*
- **What shipped:** `reva/claude_code_runner.py` runs the review/audit CLI **without** `--dangerously-skip-permissions`, with `--allowedTools "Read,Grep,Glob,Write"`, and writes the output JSON **inside the cloned repo (cwd)** instead of `/tmp`.
- **Two boundaries, both confirmed by live-CLI smoke tests:**
  1. *No skip-permissions ⇒ the allowlist is enforced.* In a smoke test the model's fall-back `Bash` call was **denied** (`permission_denials`), so Bash/Edit/network are genuinely blocked.
  2. *Claude Code confines writes to the cwd workspace.* The first smoke test (output in `/tmp`) was blocked — "the session only permits writes within the working dir" — which is also why the old code needed skip-permissions. Moving the output into the clone fixes it. A scoped `Write(<path>)` rule was tried but **this CLI ignores it in `--print` mode** (write was denied pending approval), so **bare `Write` + the cwd boundary** is the working config.
- **Net effect:** an injected instruction can read the tree and write only within the throwaway, never-pushed clone — no Bash, no writes outside the clone, no network.
- **Verify:** unit test pins no-skip-permissions + `Read,Grep,Glob,Write` + output-inside-cwd. Live CLI: `PASS (bare)`.

### A2 — Restrict the review subprocess's network egress — **M**
- **How:** run the `claude` subprocess so it can reach **only** `api.anthropic.com` (what it needs) and nothing else — via a locked-down container/network namespace or an egress proxy allowlist on the worker. (The worker itself still needs GitHub/Chat; scope the restriction to the subprocess, or split the clone+review into an egress-restricted step.)
- **Why:** even if an injection slips past A1, it can't exfiltrate repo contents or secrets to an attacker host. This is the "secret isolation + sandboxed execution" half of the consensus defense.
- **Verify:** from inside the subprocess sandbox, a request to an arbitrary host fails while Anthropic succeeds; reviews unaffected.
- **Open decision:** egress mechanism — Docker network policy vs a forward-proxy with an allowlist. I'll recommend one after a quick spike on the odoo.sh/host network setup.

### A3 — Ground-check findings against the diff/repo — **M**
- **How:** before posting, validate each finding's `file_path`/`line_start` actually exist in the changed files (full-review: in the repo). Drop or down-rank findings that reference nonexistent locations; log them.
- **Why:** an injected instruction that tries to make REVA emit attacker-chosen text, and ordinary hallucinations, both tend to cite locations that don't exist. Cheap output-side guardrail that also improves precision.
- **Verify:** a finding citing a missing file/line is dropped; valid findings pass through.

### A4 — Audit-log admin actions — **S–M**
- **How:** record who/what/when for the privileged `/api/v1` actions (requeue, manual review, trigger audit, weekly-report) — a small `admin_audit` table written from the route handlers, plus the caller identity (API key id / source).
- **Why:** forensics + accountability for the actions that spend money and post to GitHub; today they're only in transient logs.
- **Verify:** each admin action writes one audit row; visible via API/TUI.

---

## B — Observability & telemetry

Today: `structlog` JSON logs + Google Chat alerts + a TUI reading Postgres. Missing the standard production-LLM-app pillars (errors, traces, metrics).

### B1 — Error tracking (Sentry / self-hosted GlitchTip) — **S–M**  *(do first in this section)*
- **How:** add the Sentry SDK to api, worker, scheduler; capture unhandled exceptions + `PermanentError`s with request/job context (repo, PR, run_id). Keep Google Chat for human-facing alerts.
- **Why:** grouped, de-duplicated exceptions with stack + context turn "something failed, grep the logs" into a triage queue. Highest debugging ROI for the least work.
- **Verify:** a deliberately-raised exception appears in Sentry with run context attached.

### B2 — Distributed tracing (OpenTelemetry) — **M–L**
- **How:** instrument the span chain api(webhook) → enqueue → worker(job) → Claude CLI → GitHub posts, propagating a trace id (reuse the GitHub `delivery_id` as the correlation root). Export to Tempo/Jaeger (or Sentry tracing). Use the GenAI semantic conventions for the LLM span (tokens, cost, model, latency).
- **Why:** answers "where did this review's 4 minutes go / where did it fail" across process boundaries — currently impossible with per-service logs.
- **Verify:** one webhook produces a single connected trace spanning all services.

### B3 — Metrics + dashboards (Prometheus + Grafana) — **M**
- **How:** expose `/metrics` on the api (and a pushgateway/exporter for worker/scheduler): queue depth, review p50/p95 latency, tokens & cost burn rate, cache-hit rate, failure rate. Grafana dashboards + alert rules.
- **Why:** time-series + alerting the TUI's point-in-time Postgres queries can't give (trends, burn-rate alerts, SLO tracking).
- **Verify:** dashboards populate; a synthetic queue backlog fires the alert.

> *(Langfuse / LLM-trace store is intentionally parked with the deferred eval work — it shares that infrastructure.)*

---

## C — Supply-chain & security scanning in CI  *(extends Phase-1 R9)* — **$0 on GitHub Team, no add-ons or API keys**

**Why (whole section):** R9 added ruff/mypy/golangci-lint, but nothing scans dependencies, secrets, or code for vulnerabilities — and the Phase-1 audit found a placeholder key had once been committed. Everything here runs as free tooling inside GitHub Actions; none of it needs the paid **Code Security** / **Secret Protection** add-ons (≈ $30 / $19 per active committer/month) that CodeQL and GitHub-native secret scanning require on Team — the free CLIs give equivalent coverage for a Python+Go repo this size.

- **C1 — Dependency vulns — S:** **Dependabot is free on Team (private repos included) — enable it** (alerts + security + version-update PRs); add `pip-audit` (Python) + `govulncheck` (Go) as CI gates. *Verify:* a known-vulnerable pin fails CI.
- **C2 — Secret scanning — S:** **gitleaks** in CI + a pre-commit hook (free; stands in for the paid Secret Protection add-on). *Verify:* a planted fake secret is caught.
- **C3 — SAST — S–M:** **Semgrep** (Python + Go, free OSS rules) plus `bandit` / `gosec` — chosen over CodeQL precisely because CodeQL needs the paid Code Security add-on on private Team repos. *Verify:* an intentional injection-y snippet is flagged.
- **C4 — Image scanning — S:** **Trivy** on the built images (+ dep/SBOM scan). *Verify:* a vulnerable base layer is reported.

---

## D — Testing depth  *(extends Phase-1 R9)*

**Why (whole section):** the suite is fast unit tests with mocks — good, but it can't catch cross-component bugs, and the **Postgres-only** logic we added in Phase 1 (advisory-lock budget, `FOR UPDATE SKIP LOCKED` poller) is *invisible to the SQLite tests*. That's a real coverage hole.

- **D1 — Integration tests with testcontainers — M:** real Postgres + Redis; cover migration apply, the SKIP-LOCKED poller claim under concurrency, the stale-run reaper, and the advisory-locked budget check. *Verify:* two concurrent pollers enqueue each row once against real Postgres.
- **D2 — One e2e happy path — M:** signed fake webhook → assert `pending_review` → run worker against a mocked/recorded GitHub → assert a Check Run + review were "posted". *Verify:* the pipeline produces the expected GitHub calls end-to-end.
- **D3 — API↔TUI contract — M:** generate an OpenAPI spec from FastAPI; snapshot-test it so a schema change that would break the Go client fails CI. *Verify:* a breaking response-model change reddens CI.
- **D4 — Graduate gates — S–M:** once a baseline is clean, flip mypy + golangci-lint from advisory to **blocking**. *Verify:* a new type error fails CI.

---

## E — Periodic `custom_addons` repo overview / audit  *(you requested this)*

**Why (whole section):** per-PR review catches what's *changing*; it never assesses the *standing* health of `custom_addons`. You want a periodic, branch-specific overview. REVA already has the engine for this — the `Auditor` + `reva-repo-audit` skill + `run_audit` job — but it only runs **manually** (TUI / `POST /repos/{id}/audit`) and clones the **default branch**.

### E1 — Scheduled, branch-specific audits — **M**
- **How:** let an audit target a specific branch (thread a `ref` through `Auditor`/`ensure_repo`, which already accepts a SHA), and have the **scheduler** enqueue audits on a cadence from config (`repo → [branches], interval`). Reuses the existing enqueue→worker path.
- **Why:** continuous visibility into debt/drift on the branches you care about (e.g. per-customer odoo.sh branches), without someone remembering to click.
- **Verify:** a configured repo/branch gets an audit enqueued on schedule; results land in `audit_runs`.

### E2 — Repo overview / health view in the TUI — **M**
- **How:** aggregate `audit_runs` over time into a per-repo health summary (finding counts by severity/category, trend) and add a TUI view (new tab or a Repos drill-in).
- **Why:** turns individual audits into a trend you can act on.
- **Verify:** the view renders current + trend for an audited repo.

### E3 — Engine choice — **decision needed**
- **What I recommend:** build on REVA's **own** LLM audit first (E1/E2) — it's repo-aware and already integrated — and optionally add **`pylint-odoo`** (OCA's Odoo-specific static linter) as a cheap, deterministic complement whose results feed the same overview.
- **Open question for you:** you mentioned **"codeify or a similar tool"** — I'm not sure which product that is. Candidates do quite different things: **CodeScene** (behavioral hotspots / health score), **SonarQube/Sonar** (quality gates), **Codacy/Code Climate** (maintainability dashboards), **pylint-odoo** (Odoo rules). Tell me which "codeify" is (or which capability you actually want — *health score*, *quality gate*, or *Odoo-lint*) and I'll fold the right one into E.

---

## F — Data governance

- **F1 — Retention/purge job — S–M:** *How:* a scheduled purge of review data older than a configurable window, and a policy for author-login PII. *Why:* privacy/compliance + bounded DB growth. *Verify:* rows past the window are purged; recent data untouched.
- **F2 — Data-handling note — S:** *How:* document that diffs/code are sent to the Anthropic API and the retention posture. *Why:* answers the inevitable "where does our code go" question. *Verify:* doc exists and is accurate.

---

## Suggested sequence
1. **C1–C3** (security scanning) — $0 on Team, no new infra/keys, no runtime behaviour change. Safest first PR. *(C4 Trivy is an easy optional add — kept lean to start, per "few tools.")*
2. **A1** (scope the `Write` tool) — small, but needs the (i)/(ii) decision + a check against the live `claude` CLI first.
3. **A2** (lock subprocess egress) — pending the egress-mechanism decision.
4. **B1** (error tracking — GlitchTip self-hosted) — biggest debugging ROI, once you're ready to run one extra service.
5. **A3, A4** (grounding check, admin audit log).
5. **D1–D2** (integration + e2e — close the Postgres-only coverage hole).
6. **E** (periodic audit + overview) — pending your "codeify" answer.
7. **B2/B3** (tracing, metrics), **D3/D4**, **F** — as capacity allows.

**Decisions I need from you while reviewing:** (1) A2 egress mechanism, (2) E3 "codeify"/external-tool choice, (3) error-tracking target — hosted **Sentry** vs self-hosted **GlitchTip**.
