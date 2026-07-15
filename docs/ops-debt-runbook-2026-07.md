# Ops-Debt Runbook — July 2026 Sweep

Owed operational items, packaged for manual execution (the standing rule:
prod lifecycle ops are triggered by a human, not an agent). Work top to
bottom; each item says how to verify it's done. Strike items here as they
close — this file is the tracker.

Prod: `ssh -i ~/.ssh/id_work reva_service@157.90.243.66`, repo at
`~/cu_reva`, canonical deploy is `scripts/deploy.sh`.

## 1. Redeploy prod api + worker

The TUI binary on prod is newer than the services: the journey pane 404s
until api/worker are rebuilt (shipped 2026-07-10), and everything since
(instance-scoped GETs, github_url, repo-docs grounding, migrations 036–040,
ticket-actuals) is undeployed.

```bash
ssh -i ~/.ssh/id_work reva_service@157.90.243.66
cd ~/cu_reva && ./scripts/deploy.sh
```

**Verify:** `docker compose ps` all healthy; migrations applied through 040
(`psql … -c "select max(version) from schema_migrations"` or the boot log);
TUI journey pane renders a timeline instead of 404.

## 2. Confirm REVA_DEFAULT_MODEL supports structured outputs

`strict:true` on ticket analysis is ineffective if prod pins a
non-structured-outputs model; the `def127e` retry only helps once deployed
(item 1 covers the deploy).

```bash
grep REVA_DEFAULT_MODEL ~/cu_reva/.env
```

**Verify:** the model is a structured-outputs-capable pin. Then run one
ticket analysis and check the run row has no schema-retry error. (Closes the
`ticket-analysis strict gap` note.)

## 3. Staging gates on shipped features

- **Repo-docs grounding (2026-07-14):** run one real ticket against a repo
  with addon READMEs → the *Existing Customizations* HTML section appears,
  TUI shows `repo docs:N`, and a second run takes the `fresh` fast path.
- **Issue-conformance check (migration 036):** one live review of a PR
  linked to a REVA-created issue → intent_check populated, journey links it.
- **Ticket actuals (2026-07-15):** blocked on the ast-odoo sender
  (`2026-07-15-ticket-actuals-sender-design.md`). Once shipped: mark one
  staged ticket done → `ticket_actuals` row exists, journey shows
  `actuals_reported`.

## 4. Cloudflare Access on /docs + /repo-docs

The nginx template assumes an edge Access app that was never created; the
docs SPA is currently reachable by anyone with the hostname.

Zero Trust dashboard → Access → Applications → Add (self-hosted):

- Application domain: `reva.dev.cloudunify.org`, paths `/docs` and
  `/repo-docs` (two path rules on one app, or two apps).
- **Do not** cover `/webhooks` (GitHub can't SSO), `/api`, or `/health`.
- Policy: allow the Cloudunify org (email domain), session length to taste.
- If the Odoo→REVA path ever gets gated too, the connector already sends
  `CF-Access-Client-Id/Secret` service-token headers when configured —
  create a service token and set the two params in Odoo Settings → REVA.

**Verify:** incognito `https://reva.dev.cloudunify.org/docs/` → Access login;
webhook deliveries in the GitHub App dashboard still return 2xx afterwards.

## 5. Bearer swap — verify only (client side already done)

The connector already sends `Authorization: Bearer <instance key>`
(`reva_connector_mixin._reva_headers`, checked 2026-07-15) — the 2026-07-01
"swap pending" note is stale. Remaining check: each prod Odoo instance's
`reva.api_key` param holds its own **per-instance** key (not the master
key), and `GET /api/v1/health` with that key authenticates as the instance.

## Closed

- ~~Migrations 036–040 raw SQL on real Postgres~~ — exercised locally
  2026-07-15 via `make test-integration` (`db.migrate()` against a real
  container). Prod application happens at boot in item 1.
