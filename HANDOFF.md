# REVA — Work Handoff

**Updated:** 2026-06-02. Resume point for the production-readiness work.
**Replaces** the old slice-by-slice handoff (that described the original
Messages-API design and is now history in git).

---

## ⚠️ Read first — resuming on another workstation

Everything below is **merged to `main` and pushed** (origin in sync at `421b51f`).
There is no longer a `feature/production-readiness` branch. On a new workstation:
`git clone`/`git pull`, then recreate the per-service venvs (see "Running tests").

---

## Where things stand

**Phase 1 — reliability/security hardening (R1–R16): DONE & committed** (`a3343cf`),
tests-first. Highlights: RQ/CLI timeout alignment, git-op timeouts, stale-`running`
reaper, idempotent GitHub posting, webhook processed-flag idempotency, poller
`FOR UPDATE SKIP LOCKED`, advisory-locked budget, fail-closed API auth, CI
lint/type/coverage, backup script, prod-compose healthchecks/limits, `/health`
checks Redis, SSRF allowlist, pagination caps, rate limiter, owner/name validation.
Full detail: **`docs/production-readiness-plan.md`** (every item marked ✅).

**Phase 2 — gap-analysis follow-ups:** see **`docs/production-readiness-plan-phase2.md`**
(per-item Why/How/status). State:

| Item | Status |
|---|---|
| **A1** drop `--dangerously-skip-permissions`, output-in-clone | ✅ done, live-CLI verified (`a2e9030`) |
| **A3** ground-check findings vs the clone | ✅ done (`0ee3588`) |
| **A4** audit-log admin actions (`admin_audit` table) | ✅ done (`7b51e78`) |
| **A2** worker egress allowlist (proxy sidecar) | ✅ code done & tested; **infra (`egress-proxy/` + `docker-compose.egress.yml`) needs STAGING validation** (`43097b6`) — see `docs/egress-lockdown.md` |
| **C1–C3** free $0 security scanning (Dependabot/gitleaks/Semgrep) | ✅ done (`5ae6a09`); advisory until first run reviewed — see `docs/security-scanning-setup.md` |
| **E** CodeGraph engine layer | ✅ **spiked + implemented behind flag** (`REVA_CODEGRAPH_ENABLED`, default off); spec updated with live results. **Next = staging validation on a real PR** (below) |
| **D1–D2** integration/e2e (testcontainers) | 📋 not started — closes the Postgres-only coverage gap (SKIP-LOCKED/advisory-lock untested on SQLite) |
| **B2/B3** OTel tracing, Prometheus/Grafana · **D3/D4** · **F** data governance | 📋 later |
| **B1** error tracking (GlitchTip/Sentry) | ⏸️ backlog (parked by decision) |
| **E1/E2** human repo-overview, feedback/eval capture | ⏸️ out of scope / deferred |

Test counts: **worker 267 · api 75 · scheduler 16**, ruff clean.

---

## CodeGraph (Phase-2 E) — spiked + implemented (2026-06-01)

**Spike done** in the worker container. Results (full detail in the spec's "Spike
results" section): pinned **0.9.8** (npm `@colbymchenry/codegraph`); on a real
Odoo repo (OCA/server-tools, 464 Py files) indexing took **4s**; a steered review
was **~26% cheaper / ~72% fewer tool calls** than grep-only. Corrected the spec's
wrong guesses — it's `codegraph init`/`sync` + `codegraph serve --mcp` (not
`codegraph mcp`). **Key finding: steering is mandatory** — without a prompt note
the model ignores the tools and greps.

**Implemented (behind `REVA_CODEGRAPH_ENABLED`, default off):** `_codegraph_prepare`
+ gating in `reva/claude_code_runner.py` (repo-aware skills only: `reva-full-review`,
`reva-repo-audit`; diff/delta excluded; fall back on any index/setup failure);
settings (`worker/worker/settings.py`); Dockerfile pin; steering notes in the two
repo-aware skills; `.env.example`. Unit tests cover the gating matrix + fallback.

**👉 Next = staging validation on a real PR:** set `REVA_CODEGRAPH_ENABLED=true`
on a staging worker, run a `full`/`deep` review on one repo, confirm the model
calls `mcp__codegraph__*` and the review still completes; then enable for `deep`
first, then `full`/`audit`. Keep `diff` off CodeGraph. (Same live-CLI gate as A1/A2.)
If you also run reviews under the A2 egress lock, the codegraph MCP server is a
local stdio subprocess (no egress) — nothing to allowlist.

---

## Review scope & triggers (2026-06-02)

Shipped this session (all on `main`):

- **`/review-all`** comment command → `diff-all` mode: a diff-depth review (Sonnet)
  over **all** changed files, not just `custom_addons/`. Reuses the command →
  `review_mode` channel; reviewer passes `include_prefixes=()` for that mode.
- **Default diff cap raised 1000 → 2500 lines** (`DEFAULT_MAX_DIFF_LINES`). Token
  guard (60k) and per-repo `.claude-review.yml max_diff_lines` override unchanged.
- **Test-dir exclusion: tried then reverted** (`323b716` → `a21adc9`). Decision:
  **keep test files in all modes.** Don't re-add a global `tests/` skip; if a repo
  wants it, use `skip_paths: ["*/tests/*"]` in its `.claude-review.yml`.

**👉 Next step — comment commands on unknown PRs.** Comment commands only act on
PRs REVA already has a row for (registered from a prior `pull_request` event); a
PR opened before install logs `comment_trigger_pr_not_found` and is ignored
(`_handle_issue_comment` only *looks up* the PR — `webhooks.py` → `lookup_pull_request`).
Planned fix: on a DB miss, fetch the PR from the GitHub API (the handler already
has a `github` client + installation id), upsert it, then proceed — so
`/review` / `/review-all` work on any open PR without a reopen/push. Scoped, TDD.
Until then the workaround is: reopen the PR or push a commit to register it.

---

## Operator to-dos still owed (not code)

- **Enable Dependabot** alerts + security updates: repo → Settings → Code security
  and analysis. (`docs/security-scanning-setup.md` §1.)
- **After the first CI run:** review gitleaks/Semgrep output, add allowlist entries
  for test placeholders, then delete the two `continue-on-error: true` lines in
  `.github/workflows/ci.yml` to make them blocking.
- **A2 egress overlay:** validate in staging per `docs/egress-lockdown.md` before
  using in prod (allowed host works / disallowed blocked / a real review still
  completes). If you enable the **ticket-analysis** tool, add the Odoo host to
  `egress-proxy/filter`.

---

## Decisions log (this session)

- **Topology:** single instance now, **scaling soon** → concurrency/idempotency treated as real.
- **R7 budget cap:** bounded overshoot via Postgres advisory lock (not a hard reservation).
- **A2 egress:** proxy sidecar (over fragile iptables CIDR rules — Anthropic IPs rotate).
- **E3 / CodeGraph:** **engine layer** (cheaper/repo-aware reviews via MCP), not a human dashboard; spike-first.
- **B1 error tracking:** backlog. **CD:** handled by odoo.sh. **Backups:** deferred till past testing.
- **Rejected:** committable suggested-fixes (juniors learn by doing), change "walkthrough" summaries.
- **Tooling:** keep it lean / few new services; self-host over SaaS; $0 on GitHub Team.

---

## Orientation (architecture & where things live)

Flow: GitHub webhook → `api` verifies + upserts `pending_reviews` (debounce) →
`scheduler` poller enqueues RQ → `worker` clones the repo & runs the **headless
`claude` CLI** (Sonnet/Opus) → posts Check Run + PR Review → Postgres for
analytics; Go/Bubble-Tea `tui` reads the internal `/api/v1`.

- **Authoritative docs:** root `README.md`, per-module `*/README.md`,
  `docs/superpowers/specs/`, and the two `docs/production-readiness-plan*.md`.
  The numbered `doc/00–13` are **legacy** (original Messages-API design).
- **Shared lib:** `reva/` (types, clients, db, `claude_code_runner.py`, formatters).
- **Run tests** (per service, Python 3.14, each installs `reva` editable):
  ```bash
  cd worker && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest tests/   # 267
  cd ../api && … pytest tests/        # 75
  cd ../scheduler && … pytest tests/  # 16
  ```
  Lint: `ruff check reva worker/worker api/app scheduler/scheduler`. Go TUI: `cd tui && go test ./...`.
- **Live-CLI validation pattern:** A1/A2 were verified by running the real `claude`
  CLI in the worker container (the sandbox can't). CodeGraph's spike is the same idea.
