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

### A2 — Restrict the worker's network egress to an allowlist — **M**  ✅ *code done; infra delivered, pending staging validation*
- **Approach (decided):** allowlisting **proxy sidecar** — robust domain-based egress (plain iptables can't, since Anthropic's IPs rotate and would break reviews). Smoke test confirmed the `claude` CLI honours `HTTPS_PROXY`.
- **What shipped:**
  - *Code (tested):* `_ENV_ALLOWLIST` now forwards `HTTP(S)_PROXY`/`NO_PROXY` into the CLI subprocess — without this the subprocess wouldn't route through the proxy and inference would break under a hard block.
  - *Infra (validate in staging):* `egress-proxy/` (alpine + tinyproxy, default-deny + allowlist filter), `docker-compose.egress.yml` overlay (worker `*_PROXY` env), `docs/egress-lockdown.md`.
- **Allowlist:** anthropic, github.com / api.github.com / codeload, chat.googleapis.com; Odoo host is a documented opt-in (ticket analysis only — *not* PR reviews).
- **Advisory vs enforcing:** the overlay routes via the proxy (compliant clients); the doc covers the hard-block step (internal network) for when you want it. A1 already closes the primary vector, so advisory is a reasonable first layer.
- **Verify:** unit test (proxy env forwarded) + the staging checklist in the doc (allowed host works, disallowed blocked, a real review still completes). Can't be CI-validated (needs a live Docker host).

### A3 — Ground-check findings against the clone — **M**  ✅ *done*
- **What shipped:** `reviewer._ground_findings` drops any finding whose `file` doesn't exist in the cloned repo (or escapes it via `../`), before capping. Works for all modes (the CLI can read any repo file, so grounding is against the clone, not the diff). General (no-file) findings are kept; **fail-open** if the clone path is absent (drop nothing rather than nuke all).
- **Why:** injected/hallucinated findings tend to cite nonexistent locations — cheap output-side guardrail that improves precision *and* limits what an injection can put on the PR.
- **Verify:** tests — ungrounded + path-traversal findings dropped when the clone is present; nothing dropped when it's absent. Worker suite green (244).

### A4 — Audit-log admin actions — **S–M**  ✅ *done*
- **What shipped:** new `admin_audit` table (migration `008` + model) and `writers.record_admin_action`. The four privileged `/api/v1` actions — `requeue`, `manual_review`, `audit`, `weekly_report` — each write a row with action, target, `detail`, and the caller's IP (`actor_from_request`, via nginx's `X-Forwarded-For`/`X-Real-IP`).
- **Why:** forensics + accountability for actions that spend money and post to GitHub; previously only in transient logs. (Actor is the source IP — the API has one shared key, so per-user identity isn't available.)
- **Verify:** writer unit test + API test (a `/admin/review` POST writes a `manual_review` row with target+actor). Suites green (api 69, worker 245).

---

## B — Observability & telemetry

Today: `structlog` JSON logs + Google Chat alerts + a TUI reading Postgres. Missing the standard production-LLM-app pillars (errors, traces, metrics).

### B1 — Error tracking (Sentry / self-hosted GlitchTip) — **S–M**  ⏸️ *BACKLOG (parked by decision)*
- Parked for now — revisit later. (When picked up: self-hosted GlitchTip, Sentry SDK in api/worker/scheduler, capture unhandled exceptions + `PermanentError`s with run context; Google Chat stays for human alerts.)

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

## E — Periodic `custom_addons` audit + CodeGraph  *(you requested this; E3 = CodeGraph)*

**Why (whole section):** per-PR review catches what's *changing*; it never assesses the *standing* state of `custom_addons`. REVA already has the audit engine — `Auditor` + `reva-repo-audit` skill + `run_audit` — but it only runs **manually** (TUI / `POST /repos/{id}/audit`) and clones the **default branch**.

### E1 — Scheduled, branch-specific audits — **M**
- **How:** let an audit target a specific branch (thread a `ref` through `Auditor`/`ensure_repo`, which already accepts a SHA), and have the **scheduler** enqueue audits on a cadence from config (`repo → [branches], interval`). Reuses the existing enqueue→worker path.
- **Why:** continuous visibility into debt/drift on the branches you care about (per-customer odoo.sh branches), without someone remembering to click.
- **Verify:** a configured repo/branch gets an audit enqueued on schedule; results land in `audit_runs`.

### E2 — Repo overview / health view in the TUI — **M**
- **How:** aggregate `audit_runs` over time into a per-repo summary (finding counts by severity/category, trend); add a TUI view (new tab or a Repos drill-in).
- **Why:** turns individual audits into a trend you can act on.
- **Verify:** the view renders current + trend for an audited repo.

### E3 — CodeGraph — **clarify intent first**
CodeGraph (github.com/colbymchenry/codegraph) is a **local, pre-indexed code knowledge graph for AI coding agents** (Claude Code included). It indexes a repo with tree-sitter into a SQLite graph and serves it over MCP, so the agent queries structure instead of grepping — ~25–35% cheaper, ~57–70% fewer tool calls. It is **not** a human-facing health dashboard. Two ways it could serve the goal:
- **(E3-a) Code-intelligence layer for reviews/audits** — run CodeGraph to index the cloned repo and expose its MCP to the headless `claude` run, so **full/deep reviews + audits** are cheaper and more cross-file-aware (the repo-aware modes benefit most; the diff path may not need it). *Composes with A1/A2:* CodeGraph is local-only (no egress), but its MCP tools must be added to the `--allowedTools` allowlist.
- **(E3-b) Feed E2's overview** — use CodeGraph's structural data (module/symbol/route inventory) as input to the periodic per-repo summary, complementing REVA's LLM audit.
- **Caveats:** CodeGraph is **pre-1.0** (v0.9.5, Jan-2026 launch) — for the critical review path I'd pin a version and gate it behind a flag, starting on the audit/full-review path only, not the hot diff path. Also adds an indexer step + MCP server (local, but ops weight).
- **Decided: (a) engine layer.** Index the clone with CodeGraph and expose its MCP to the headless `claude` run so **full/deep reviews + audits** are cheaper and more cross-file-aware. Version-pinned, flag-gated, repo-aware paths only (not the hot diff path). E1/E2 (human overview) are **not** in scope for now.
- **Design-first:** this is a new feature touching the just-hardened review path (A1/A2) and a pre-1.0 dependency — it gets its own spec (brainstorm → design → plan) before code, and live-CLI/MCP validation like A1 had. Larger than the other Phase-2 items.

---

## F — Data governance

- **F1 — Retention/purge job — S–M:** *How:* a scheduled purge of review data older than a configurable window, and a policy for author-login PII. *Why:* privacy/compliance + bounded DB growth. *Verify:* rows past the window are purged; recent data untouched.
- **F2 — Data-handling note — S:** *How:* document that diffs/code are sent to the Anthropic API and the retention posture. *Why:* answers the inevitable "where does our code go" question. *Verify:* doc exists and is accurate.

---

## Suggested sequence
1. ✅ **C1–C3** (security scanning) — shipped (`5ae6a09`). *(C4 Trivy optional, deferred.)*
2. ✅ **A1** (drop skip-permissions, output in clone) — shipped & live-CLI-verified (`a2e9030`).
3. **A2** (lock subprocess egress — **Docker network policy**, decided). Compose change; validate in container.
4. **A3, A4** (grounding check, admin audit log) — self-contained, no decisions/infra needed.
5. **D1–D2** (integration + e2e — close the Postgres-only coverage hole).
6. **E1/E2 + E3 CodeGraph** — pending the E3-a/E3-b intent answer.
7. **B2/B3** (tracing, metrics), **D3/D4**, **F** — as capacity allows.
8. ⏸️ **B1** (error tracking) — **backlog**, parked by decision.

**Open decision:** E3 — CodeGraph for (a) cheaper/repo-aware reviews+audits, (b) a periodic human overview, or both. (A2 decided: Docker network policy. B1: backlogged.)
