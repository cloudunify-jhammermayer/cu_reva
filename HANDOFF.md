# REVA — Work Handoff

**Updated:** 2026-05-31. Resume point for the production-readiness work.
**Replaces** the old slice-by-slice handoff (that described the original
Messages-API design and is now history in git).

---

## ⚠️ Read first — resuming on another workstation

All work is on branch **`feature/production-readiness`**, **committed but NOT pushed**
(9 commits, local only). Before switching machines:

```bash
git push -u origin feature/production-readiness
```

Then on the new workstation: clone/fetch, `git checkout feature/production-readiness`,
recreate the per-service venvs (see "Running tests"). Nothing is merged to `main`
and no PR is open yet.

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
| **E** CodeGraph engine layer | 📋 **spec done** (`d9824d9`, `docs/superpowers/specs/2026-05-31-codegraph-engine-design.md`); **next = spike (below)** |
| **D1–D2** integration/e2e (testcontainers) | 📋 not started — closes the Postgres-only coverage gap (SKIP-LOCKED/advisory-lock untested on SQLite) |
| **B2/B3** OTel tracing, Prometheus/Grafana · **D3/D4** · **F** data governance | 📋 later |
| **B1** error tracking (GlitchTip/Sentry) | ⏸️ backlog (parked by decision) |
| **E1/E2** human repo-overview, feedback/eval capture | ⏸️ out of scope / deferred |

Test counts after Phase 2: **worker 246 · api 69 · scheduler 16**, ruff clean.

---

## 👉 Next session: CodeGraph spike (do FIRST, before any E code)

I don't yet know CodeGraph's exact CLI/MCP invocation for the installed `claude`
version, so guessing it = rework. Spike in the **worker container** (`docker exec -it cu_reva-worker-1 bash`):

1. Install/locate the pinned `codegraph` binary; note version.
2. `codegraph index <a cloned custom_addons repo>` — confirm it builds; **time it**.
3. Start its MCP server (`codegraph mcp …`) and capture the exact `--mcp-config`
   JSON schema the `claude` CLI expects.
4. Run a real review with `claude --print … --mcp-config <file> --allowedTools "Read,Grep,Glob,Write,mcp__codegraph__*"` and confirm the model actually calls
   `mcp__codegraph__*`; capture the **token / tool-call delta** vs off.
5. Sanity-check Odoo-Python coverage.

Then implement per the spec (flag `REVA_CODEGRAPH_ENABLED` default off, version-pinned,
repo-aware paths only, fall-back on failure). Unit tests for gating + fall-back can
be written here; the spike resolves the live unknowns.

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
  cd worker && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest tests/   # 246
  cd ../api && … pytest tests/        # 69
  cd ../scheduler && … pytest tests/  # 16
  ```
  Lint: `ruff check reva worker/worker api/app scheduler/scheduler`. Go TUI: `cd tui && go test ./...`.
- **Live-CLI validation pattern:** A1/A2 were verified by running the real `claude`
  CLI in the worker container (the sandbox can't). CodeGraph's spike is the same idea.
