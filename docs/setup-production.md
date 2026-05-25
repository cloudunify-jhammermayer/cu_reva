# Production Server Setup

This guide deploys REVA to a Linux VPS with a real domain, HTTPS via Let's
Encrypt, and Nginx as the reverse proxy. The stack is identical to local dev
except Nginx handles TLS termination and the PEM key is passed as a Docker
secret instead of a bind mount.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Linux VPS | 1 vCPU / 1 GB RAM minimum; 2 vCPU / 2 GB recommended |
| Docker + Docker Compose v2 | `docker compose version` must work |
| A domain name | DNS A record pointing to the server's public IP |
| Port 80 + 443 open | In your firewall / security group |
| GitHub App | Create one per the [local setup guide](setup-local.md#1-create-a-github-app), using `https://your-domain.com/webhooks/github` as the webhook URL |

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

Allow required ports (example with ufw):

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 22/tcp   # keep SSH open
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
# Domain (must match your DNS A record)
REVA_DOMAIN=reviews.your-domain.com

# GitHub App
GITHUB_APP_ID=<app-id>
GITHUB_WEBHOOK_SECRET=<webhook-secret>

# Anthropic
ANTHROPIC_API_KEY=sk-ant-api03-...

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

---

## 4. Add the GitHub App private key

```bash
mkdir -p secrets
# Copy your downloaded .pem file to:
cp /path/to/app-name.YYYY-MM-DD.private-key.pem secrets/github-app-private-key.pem
chmod 600 secrets/github-app-private-key.pem
```

The production compose passes this file as a Docker secret (`secrets:` block in
`docker-compose.prod.yml`) so it is never baked into an image layer.

---

## 5. Obtain the TLS certificate

Run this **once** before starting the full stack. Port 80 must be free.

```bash
REVA_DOMAIN=reviews.your-domain.com \
EMAIL=admin@your-domain.com \
./scripts/setup-letsencrypt.sh
```

This runs certbot in standalone mode, writes the certificate into a local
`letsencrypt/` directory, and exits. The production stack mounts that directory
read-only into the Nginx container, and a `certbot` sidecar handles renewals
automatically every 12 hours.

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
curl https://reviews.your-domain.com/health   # should return {"status":"ok"}
```

---

## 7. Set the GitHub App webhook URL

In your GitHub App settings set:

**Webhook URL:** `https://reviews.your-domain.com/webhooks/github`

Test by opening a PR — you should see a Check Run appear within
`REVA_DEBOUNCE_SECONDS` of the webhook arriving.

---

## 8. Deploying updates

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

## 9. Useful operations

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
curl -X POST https://reviews.your-domain.com/api/v1/reviews/<id>/requeue
```

---

## 10. TUI against production

Build the TUI binary once on your local machine:

```bash
cd tui
go build -o reva-tui .
```

Point it at the production API:

```bash
REVA_API_URL=https://reviews.your-domain.com/api/v1 ./reva-tui
```

The `/api/` path on the Nginx config is proxied to the api container, so no
extra auth or VPN is needed (add IP allowlist in `reva.conf.template` if you
want to restrict access).

---

## Architecture overview

```
Internet
   │
   ▼
Nginx (80/443)
   ├── /webhooks/*  ──► api:8080  (rate-limited, HMAC-verified)
   ├── /api/*       ──► api:8080  (read-only review data for TUI)
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

### Certificate not found on startup
Run `setup-letsencrypt.sh` before `docker compose ... up`. The Nginx container
will fail to start if the cert files do not exist.

### `curl: (60) SSL certificate problem` on `/health`
DNS hasn't propagated yet, or the cert was issued for a different domain. Verify
with `dig reviews.your-domain.com` and check the cert with
`openssl s_client -connect reviews.your-domain.com:443`.

### Worker shows `invalid x-api-key`
Update `ANTHROPIC_API_KEY` in `.env` then rebuild and recreate the worker:
```bash
docker compose -f docker-compose.prod.yml build worker
docker compose -f docker-compose.prod.yml up -d worker
```

### Certbot renewal silently failing
Check the certbot container logs:
```bash
docker compose -f docker-compose.prod.yml logs certbot
```
Make sure port 80 still responds to `/.well-known/acme-challenge/` — the Nginx
HTTP server block handles this automatically.

### New code not picked up after `git pull`
`docker compose up -d` without `--build` reuses old images. Always run
`deploy.sh` or explicitly run `build` before `up`.

---

## Environment variable reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `REVA_DOMAIN` | yes | — | FQDN for Nginx + Let's Encrypt (e.g. `reviews.example.com`) |
| `GITHUB_APP_ID` | yes | — | Numeric ID from the GitHub App settings page |
| `GITHUB_WEBHOOK_SECRET` | yes | — | Random string set in GitHub App webhook config |
| `ANTHROPIC_API_KEY` | yes | — | API key from console.anthropic.com |
| `POSTGRES_PASSWORD` | yes | — | Password for the `review` DB user |
| `REDIS_PASSWORD` | yes | — | Redis `requirepass` value; embedded in all `REDIS_URL` values |
| `REVA_DEBOUNCE_SECONDS` | no | 600 | Seconds to wait before enqueuing a review after a webhook |
