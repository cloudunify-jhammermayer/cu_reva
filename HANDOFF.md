# REVA — Work Handoff

**Updated:** 2026-06-14. Resume point.
**Replaces** the old slice-by-slice handoff (that described the original
Messages-API design and is now history in git).

---

## 🚀 RESUME HERE (2026-06-14 session) — roadmap Tiers 0–2

This session built a feature roadmap and worked through it. **All work is on
`main`** (see `git log`); whether it's pushed/PR'd is noted at the bottom.

**Authoritative new docs (read these first):**
- `FEATURE_ROADMAP.md` — the 6-tier roadmap (root).
- `docs/tier0-plan.md`, `docs/tier1-plan.md`, `docs/tier2-plan.md` — per-tier plans + decisions.
- `docs/tier2-detailed-plans.md` — **exhaustive, verified per-feature plans for the remaining
  Tier-2 work (features 4–9)**; the next session implements from this.
- `docs/tier0-tier1-implementation.md` — how to test Tiers 0–1 + what to expect + operator setup.
- `docs/delta-resolution-analysis.md` — diagnosis of "REVA re-reviews instead of resolving old
  comments"; features 1–2 of Tier 2 fix it.

**Shipped & committed (tests green: worker 609, api 147, scheduler 28; ruff clean):**
- **Tier 0 (done):** per-repo `block_on_severity` gating; deterministic Odoo severity calibration;
  `reva-risk-*` PR labels; trivial-diff short-circuit (+ a reorder false-skip fix).
- **Tier 1 (done):** prompt-version registry + drift guard; per-finding outcome ledger (migration
  `015`); feedback capture via `pull_request_review_thread`.
- **Tier 2 (9 of 9 done):** force-push/rebase delta-base guard; **delta-aware finding suppression**
  (the headline bug fix); test-coverage gate; **intent-grounded review** (4); **`__manifest__.py`
  validator** (5, `reva/odoo_manifest.py`); **second-pass self-critique** (6, default-off behind
  `REVA_VERIFY_HIGH_COST`); **migration-safety skill** (7, `reva-migration-review`); **XML/QWeb skill**
  (8, `.xml` un-stripped + `reva-xml-review`); **security-model consistency** (9, cross-check + floor).
  Routing is centralized in `reviewer._select_skill` (migration > delta > xml-only > diff/full).

**⏭️ NEXT: Tier 3 (self-improvement)** per `FEATURE_ROADMAP.md` — now unblocked by the Tier-1 data loop
(feedback capture + outcome ledger). But **first run the live-CLI staging gate** for the Tier-2 review
features (below) before trusting their output in prod.

**Owed staging validation (live-CLI gate, A1/A2 pattern) for Tier 2 features 4–9** — unit suites prove
plumbing/routing only; review *quality* must be validated on a real Odoo repo:
- (4) intent-grounding false-positive rate; (5) the LLM `depends`/version judgement + how the model
  merges the deterministic `manifest_audit` param; (6) the self-critique **false-drop rate** (enable
  `REVA_VERIFY_HIGH_COST` on staging first); (7) migration checks (destructive DDL, idempotency, JSONB);
  (8) xpath/`inherit_id` resolution (false positives on valid xpath); (9) detection quality — esp. **not**
  false-flagging the `_inherit`-extension pattern (the most common Odoo change).

**Prompt versioning:** all Tier-2 prompt/skill edits land under CHANGELOG version **v1.6** (the
drift guard from Tier 1 will alert if a prompt changes without a version bump; `test_get_version`
asserts the current string).

**Operator actions owed (not code):**
- **Enable the `Pull request review thread` webhook event** on the GitHub App — until then Tier-1
  feedback capture is dormant (no permission change needed; uses `Pull requests: Read`).
- The features 1–2 bug fix is partly prompt-quality (`reva-delta-review.md` guidance) — **validate on a
  real re-review PR on staging** before fully trusting it (the A1/A2/CodeGraph live-gate pattern).

**Local venvs:** `api/.venv` and `scheduler/.venv` were created this session; if missing, recreate per
"Running tests". `ruff` was pip-installed into `worker/.venv` for local linting.

---

## ⚠️ Read first — resuming on another workstation

The 2026-06-14 work above is on `main`. On a new workstation: `git clone`/`git pull`, then recreate
the per-service venvs (see "Running tests"). The pre-2026-06-14 baseline below was in sync at `499da2e`.

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

Test counts (at the time of Phase 2): worker 267 · api 75 · scheduler 16.

---

## Recent work (2026-06-03 → 06-05) — on `main`, deployed locally, NOT yet in prod

Test counts now: **worker 339 · api 98 · scheduler 27**, ruff clean, **CI green**.

- **Repo audits — completed.** Findings are persisted (`audit_findings` table) and **major/critical are opened as GitHub issues** (`[REVA audit] …`, auto-created `reva-audit` label, hidden-marker dedup). Read via `GET /api/v1/audit-findings` + the new **TUI Audits tab (`8`)**; trigger from the **Repos tab (`a`)** or the API. Audits run on the **deep model**. Requires GitHub App **Issues: Read & write**.
- **Comment auto-resolution — fixed.** Backfill used `/pulls/{pr}/reviews/{id}/comments` (returns `line:null`), so `github_comment_id` was never stored and delta re-reviews resolved nothing (Aurium #60). Now uses the PR-level `/pulls/{pr}/comments` endpoint filtered by `pull_request_review_id`. Added `finding_comment_ids_*` / `delta_resolution_*` logs.
- **Models env-configurable.** Single source `reva/config.py`: `REVA_DEFAULT_MODEL` (`claude-sonnet-4-6`), `REVA_DEEP_MODEL` (**`claude-opus-4-8`**, bumped from 4-7). Wired through both compose files. Audits + `/deep-review` use the deep model.
- **CodeGraph enabled** on the live worker (`REVA_CODEGRAPH_ENABLED=true`) + a positive `codegraph_index_ready` log. **Still owed (HANDOFF's standing CodeGraph gate): confirm the model actually calls `mcp__codegraph__*` on a real full/deep PR.**
- **nginx → Cloudflare tunnel.** Plain HTTP on `127.0.0.1:8080`; TLS at the Cloudflare edge; real client IP via `CF-Connecting-IP`. Dropped certbot / Let's Encrypt / `:443` / `setup-letsencrypt.sh`. Added a branded cloud **404**. Prod compose + `docs/setup-production.md` rewritten for the tunnel.

**👉 What's next — see [`docs/next-steps-plan.md`](docs/next-steps-plan.md)** (per-item P0/P1/P2 plans + a pre-deploy live-test checklist). Highest value: **deploy to the server and run the live-test checklist** — the only way to prove the tunnel, CodeGraph-in-use, the resolution cycle, and audits end-to-end. (The two **P0** bugs once listed here — comment commands on unknown PRs, and the deep→diff downgrade CORR-7 — are now both fixed; see the resume section above.)

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

**✅ Resolved — comment commands on unknown PRs.** On a DB miss `_handle_issue_comment`
now fetches the PR from the GitHub API and upserts it (`_fetch_and_upsert_pr`,
`webhooks.py`), so `/review` / `/review-all` work on any open PR without a reopen/push
(fixed in `004cd5c`, tested). The CORR-7 `deep→diff` downgrade is likewise fixed
(`_MODE_PRECEDENCE` guard in `upsert_pending_review`, `11e601a`).

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
- **TLS/ingress (2026-06-05):** Cloudflare tunnel — TLS at the edge, nginx plain-HTTP on `127.0.0.1:8080`, no certs/certbot. Over the old Let's Encrypt+`:443` setup.
- **Deep model (2026-06-05):** `claude-opus-4-8` (from 4-7), env-overridable; **audits always use the deep model**.
- **Audits (2026-06-05):** **manual-only** (API/TUI trigger); periodic/cron audits deferred — deliberate, revisit if needed.

---

## Orientation (architecture & where things live)

Flow: GitHub webhook → `api` verifies + upserts `pending_reviews` (debounce) →
`scheduler` poller enqueues RQ → `worker` clones the repo & runs the **headless
`claude` CLI** (Sonnet/Opus) → posts Check Run + PR Review → Postgres for
analytics; Go/Bubble-Tea `tui` reads the internal `/api/v1`.

- **Authoritative docs:** root `README.md`, per-module `*/README.md`,
  `docs/superpowers/specs/`, and the two `docs/production-readiness-plan*.md`.
  (The legacy numbered `doc/00–13` tree has been removed.)
- **Shared lib:** `reva/` (types, clients, db, `claude_code_runner.py`, formatters).
- **Run tests** (per service, Python 3.14, each installs `reva` editable):
  ```bash
  cd worker && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest tests/   # 339
  cd ../api && … pytest tests/        # 98
  cd ../scheduler && … pytest tests/  # 27
  ```
  Lint: `ruff check reva worker/worker api/app scheduler/scheduler`. Go TUI: `cd tui && go test ./...`.
- **Live-CLI validation pattern:** A1/A2 were verified by running the real `claude`
  CLI in the worker container (the sandbox can't). CodeGraph's spike is the same idea.
