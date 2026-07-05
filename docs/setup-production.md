# Production Server Setup

This guide deploys REVA to a Linux server behind a **Cloudflare tunnel**.
Cloudflare terminates TLS at its edge and a `cloudflared` connector (running on
the host) forwards `reva.dev.cloudunify.org` to nginx on `127.0.0.1:80` — so
**no public ports, no certificates, and no inbound firewall rules** are needed.
nginx runs plain HTTP as the internal reverse proxy; the PEM key and other
secrets are passed as Docker secret files instead of bind mounts / env vars.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Linux VPS | 1 vCPU / 1 GB RAM minimum; 2 vCPU / 2 GB recommended |
| Docker + Docker Compose v2 | `docker compose version` must work |
| Cloudflare account | Managing the `dev.cloudunify.org` zone |
| A Cloudflare tunnel + `cloudflared` on the host | Routes `reva.dev.cloudunify.org` → `http://localhost:80`. **No public IP / DNS A record / open ports needed** — the tunnel connects outbound. |
| GitHub App | Create one per the [local setup guide](setup-local.md#1-create-a-github-app), using `https://reva.dev.cloudunify.org/webhooks/github` as the webhook URL |

---

## 1. Server preparation

```bash
# Install Docker (Ubuntu/Debian)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# Verify
docker compose version
```

The Cloudflare tunnel connects **outbound**, so you don't open any inbound web
ports — keep only SSH:

```bash
sudo ufw allow 22/tcp   # SSH only; no 80/443 needed
sudo ufw enable
```

---

## 2. Clone the repository

```bash
git clone https://github.com/your-org/cu_reva.git /opt/reva
cd /opt/reva
```

---

## 3. Configure environment

```bash
cp .env.example .env
nano .env   # or vim/whatever you prefer
```

Required values:

```dotenv
# Domain served via the Cloudflare tunnel (nginx server_name)
REVA_DOMAIN=reva.dev.cloudunify.org

# GitHub App
GITHUB_APP_ID=<app-id>

# Database
POSTGRES_PASSWORD=<generate-a-strong-password>

# Redis
REDIS_PASSWORD=<generate-a-strong-password>

# Debounce — 600s (10 min) is the production default; tune to taste
REVA_DEBOUNCE_SECONDS=600
```

Generate a strong Postgres password if you need one:

```bash
openssl rand -hex 32
```

> The webhook secret, REVA API key, Anthropic key, and GitHub private key are
> **not** set in `.env` in production — they are injected as Docker secret
> **files** (see the next step), so they never land in an image layer or
> `docker inspect`. The `*_FILE` env vars in `docker-compose.prod.yml` point the
> services at them.

---

## 4. Create the secret files

The production compose mounts five Docker secrets from `./secrets/` (see the
`secrets:` block in `docker-compose.prod.yml`). All five are **required** —
`docker compose up` fails if a source file is missing, and the API/worker
**fail closed** (refuse to start) if a secret is empty or whitespace-only. Write
real, non-empty values:

```bash
mkdir -p secrets

# 1. GitHub App private key (the .pem you downloaded from the App settings)
cp /path/to/app-name.YYYY-MM-DD.private-key.pem secrets/github-app-private-key.pem

# 2. GitHub webhook secret — must match the value you set in the GitHub App
#    webhook config (step 7). Generate one and reuse it there:
openssl rand -hex 32 > secrets/github_webhook_secret

# 3. REVA API key — bearer token the TUI and admin/requeue endpoints must send.
#    Production fails closed without it (REVA_REQUIRE_API_KEY=true).
openssl rand -hex 32 > secrets/reva_api_key

# 4. Anthropic API key (from console.anthropic.com) — note: NO trailing newline,
#    so use printf, not echo:
printf '%s' 'sk-ant-api03-...' > secrets/anthropic_api_key

# 5. REVA secret key — Fernet key encrypting Odoo instances' callback API keys
#    at rest. Must be a valid Fernet key (NOT openssl rand); rotating it makes
#    existing encrypted callback keys undecryptable:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > secrets/reva_secret_key

chmod 600 secrets/*
```

Keep `secrets/reva_api_key` — you need it for the TUI and the `/api/v1` admin
calls (`Authorization: Bearer <that value>`).

---

## 5. Set up the Cloudflare tunnel

TLS is handled by Cloudflare — there are no certificates on the server. Install
`cloudflared` and create a tunnel that forwards `reva.dev.cloudunify.org` to
nginx (published on `127.0.0.1:80` and `[::1]:80` by the prod compose).

```bash
# Install cloudflared (Debian/Ubuntu)
curl -fsSL https://pkg.cloudflare.com/cloudflared.deb -o cloudflared.deb && sudo dpkg -i cloudflared.deb

# Authenticate + create the tunnel
cloudflared tunnel login
cloudflared tunnel create reva
cloudflared tunnel route dns reva reva.dev.cloudunify.org
```

Create `~/.cloudflared/config.yml` pointing the hostname at nginx:

```yaml
tunnel: reva
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: reva.dev.cloudunify.org
    service: http://localhost:80
  - service: http_status:404
```

Run it as a service so it survives reboots:

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

(You can also use a token-based tunnel from the Zero Trust dashboard:
`cloudflared service install <TOKEN>` with the ingress configured in the UI.)

Gate the docs surface with Cloudflare Access. Create a Cloudflare Access
application for `https://$REVA_DOMAIN` covering the paths `/docs` and
`/repo-docs` (the consultant docs SPA plus its data API). Leave `/webhooks`
(GitHub cannot SSO), `/api`, and `/health` ungated. Until this application
exists, the docs site is reachable by anyone who can reach the tunnel hostname.

---

## 6. Start the stack

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Or use the Makefile:

```bash
make build   # builds all images
make prod    # starts detached
```

Check that everything came up:

```bash
docker compose -f docker-compose.prod.yml ps
curl https://reva.dev.cloudunify.org/health   # should return {"status":"ok"}
```

---

## 7. Optional: Odoo core knowledge

Core knowledge lets PR reviews and ticket analyses compare custom work against
operator-provided Odoo core, enterprise, and official documentation checkouts.

One-time on the host:

```bash
sudo mkdir -p /srv/odoo-mirrors /srv/reva-core
git clone --no-checkout https://github.com/odoo/odoo /srv/odoo-mirrors/odoo
git clone --no-checkout <enterprise-remote> /srv/odoo-mirrors/enterprise
git clone --no-checkout https://github.com/odoo/documentation /srv/odoo-mirrors/documentation
```

Load the versions you want:

```bash
scripts/core_sync.sh 17.0 18.0 19.0
```

Then set these in `.env` and restart the worker:

```dotenv
REVA_CORE_KNOWLEDGE_ENABLED=true
REVA_CORE_VERSIONS=17.0,18.0,19.0
REVA_CORE_HOST_DIR=/srv/reva-core
```

The worker validates every listed version at boot and refuses to start if a
worktree, catalog, or registry load is missing. Per repo, set
`.claude-review.yml: odoo_version: "19.0"`. Per Odoo instance, set
`odoo_version` with `PATCH /api/v1/odoo-instances/{id}`.

---

## 8. Set the GitHub App webhook URL

In your GitHub App settings set:

**Webhook URL:** `https://reva.dev.cloudunify.org/webhooks/github`

Test by opening a PR — you should see a Check Run appear within
`REVA_DEBOUNCE_SECONDS` of the webhook arriving.

> The GitHub App also needs the **Issues: Read & write** repository permission so
> repo audits can open issues (see the
> [permissions table in the local guide](setup-local.md#1-create-a-github-app)).

> Scanner feed requires the GitHub App to have read access to **Code scanning
> alerts**, **Dependabot alerts**, and **Secret scanning alerts**. After changing
> App permissions, accept the permission update on the installation. Until then
> the scanner feed is a visible no-op via `scanner_feed / sources_unavailable`
> ops events.

### Trigger a repository audit

An audit reviews the whole repo on the default branch (deep model, Opus 4.8),
stores findings, and opens GitHub issues (label `reva-audit`) for major/critical
findings. Trigger one with:

```bash
curl -X POST https://reva.dev.cloudunify.org/api/v1/repos/<repository_id>/audit
```

or from the TUI **Repos** tab (press `a`). Read findings via
`GET /api/v1/audit-findings` or the TUI **Audits** tab (key `8`).

---

## 9. Deploying updates

```bash
cd /opt/reva
./scripts/deploy.sh
```

The deploy script:
1. `git pull origin main`
2. Rebuilds all images
3. Stops api / scheduler / worker / nginx (postgres and redis stay up — zero data loss)
4. Starts everything back up
5. Health-checks the API and prints final status

---

## 10. Useful operations

```bash
# Tail all logs (prod compose)
docker compose -f docker-compose.prod.yml logs -f

# Tail one service
docker compose -f docker-compose.prod.yml logs -f worker

# Shell into api container
docker compose -f docker-compose.prod.yml exec api sh

# Postgres REPL
docker compose -f docker-compose.prod.yml exec postgres psql -U review reviews

# Scale to 3 concurrent workers (large review load)
docker compose -f docker-compose.prod.yml up -d --scale worker=3

# Restart one service after a hotfix (no full deploy)
docker compose -f docker-compose.prod.yml build worker
docker compose -f docker-compose.prod.yml up -d worker

# Requeue a failed review via API
curl -X POST https://reva.dev.cloudunify.org/api/v1/reviews/<id>/requeue
```

---

## 11. TUI against production

Build the TUI binary once on your local machine:

```bash
cd tui
go build -o reva-tui .
```

Point it at the production API. Prod **requires** the API key
(`REVA_REQUIRE_API_KEY=true`), so pass the value you put in
`secrets/reva_api_key`:

```bash
REVA_API_URL=https://reva.dev.cloudunify.org/api/v1 \
REVA_API_KEY=<contents of secrets/reva_api_key> \
./reva-tui
```

The `/api/` path is proxied through the tunnel to the api container; access is
gated by that Bearer token (no VPN needed). Cloudflare Access can add an extra
auth layer at the edge if you want.

---

## Architecture overview

```
Internet ──► Cloudflare edge (TLS) ──► cloudflared tunnel (outbound)
                                              │
                                              ▼
                                   nginx (127.0.0.1:80, plain HTTP)
   ├── /webhooks/*  ──► api:8080  (rate-limited, HMAC-verified, GitHub-IP allowlist via CF-Connecting-IP)
   ├── /api/*       ──► api:8080  (review data for the TUI; Bearer auth)
   └── /health      ──► api:8080
         │
         ▼
      api container
      ├── POST /webhooks/github  → writes pending_reviews row
      └── GET  /api/v1/*         → reads review data

      scheduler container (polls every 30s)
      └── reads pending_reviews → enqueues RQ jobs

      worker container (RQ)
      └── run_review job
          ├── fetches PR diff via GitHub API (installation token)
          ├── calls Claude Messages API
          └── posts Check Run + PR Review to GitHub

      postgres:5432  (data persisted in Docker volume)
      redis:6379     (RQ job queue)
```

---

## Common problems

### Tunnel up but site returns 502 / 404
`cloudflared` reaches the host but not nginx. Check the ingress points at
`http://localhost:80` and nginx is healthy:
```bash
sudo cloudflared tunnel info reva
curl -s http://localhost:80/nginx-health   # should print "ok"
docker compose -f docker-compose.prod.yml ps nginx
```

### Webhooks rejected (403) at nginx
The `/webhooks/` allowlist matches the real client IP restored from
`CF-Connecting-IP`. If GitHub hooks are denied, confirm Cloudflare is forwarding
that header (it is by default through the tunnel) and that GitHub's hook CIDRs in
`nginx/templates/reva.conf.template` are current (https://api.github.com/meta).

### Worker shows `invalid x-api-key`
Fix the value in `secrets/anthropic_api_key` (no trailing newline — use
`printf`), then recreate the worker so it re-reads the secret:
```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate worker
```

### New code not picked up after `git pull`
`docker compose up -d` without `--build` reuses old images. Always run
`deploy.sh` or explicitly run `build` before `up`.

---

## Environment variable reference

In production the four secrets below are supplied as **files** under `secrets/`
(via the `*_FILE` env vars), not as plain env vars — see step 4. The rest go in
`.env`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `REVA_DOMAIN` | yes | — | FQDN served via the Cloudflare tunnel; nginx `server_name` (e.g. `reva.dev.cloudunify.org`) |
| `GITHUB_APP_ID` | yes | — | Numeric ID from the GitHub App settings page |
| `GITHUB_WEBHOOK_SECRET` | yes (file) | — | `secrets/github_webhook_secret`; must match the GitHub App webhook config |
| `REVA_API_KEY` | yes (file) | — | `secrets/reva_api_key`; bearer token for the TUI + `/api/v1` admin endpoints. Prod fails closed without it (`REVA_REQUIRE_API_KEY=true`) |
| `ANTHROPIC_API_KEY` | yes (file) | — | `secrets/anthropic_api_key`; key from console.anthropic.com |
| GitHub private key | yes (file) | — | `secrets/github-app-private-key.pem` (downloaded from the App settings) |
| `POSTGRES_PASSWORD` | yes | — | Password for the `review` DB user |
| `REDIS_PASSWORD` | yes | — | Redis `requirepass` value; embedded in all `REDIS_URL` values |
| `REVA_DEBOUNCE_SECONDS` | no | 600 | Seconds to wait before enqueuing a review after a webhook |
| `REVA_DEFAULT_MODEL` | no | `claude-sonnet-4-6` | Model for diff/full reviews, ticket analysis, and comment replies |
| `REVA_DEEP_MODEL` | no | `claude-opus-4-8` | Model for `/deep-review` and all repo audits |
| `REVA_CODEGRAPH_ENABLED` | no | `false` | When `true`, repo-aware reviews (full/deep) and audits get a pre-indexed CodeGraph exposed via MCP. Requires the `codegraph` binary in the worker image (already pinned in the Dockerfile) |
| `REVA_CODEGRAPH_INDEX_TIMEOUT` | no | 180 | Seconds bounding the CodeGraph index step |
