# Local Development Setup

This guide gets REVA running on your local machine so you can test the full
webhook → review pipeline end-to-end. You need Docker, a public ngrok URL for
GitHub to reach, and a GitHub App.

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker + Docker Compose v2 | any recent | `docker compose version` must work |
| Go | 1.21+ | for the TUI binary |
| ngrok | v3 | free tier is fine |
| GitHub account | — | to create the GitHub App |

---

## 1. Create a GitHub App

Go to **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**.

| Field | Value |
|---|---|
| App name | anything (e.g. `reva-local`) |
| Homepage URL | `http://localhost:8080` |
| Webhook URL | leave blank for now — you'll fill it in after ngrok starts |
| Webhook secret | generate a random string, e.g. `openssl rand -hex 20` |

**Repository permissions:**

| Permission | Level |
|---|---|
| Checks | Read & write |
| Contents | Read-only |
| Pull requests | Read & write |

**Subscribe to events:** tick **Pull requests**.

After creating the app:
1. Note the **App ID** shown at the top of the app settings page.
2. Scroll to **Private keys** → **Generate a private key** → save the downloaded `.pem`.
3. Install the app on the repos you want to review (Settings → Install App).
   - Note the **Installation ID** from the URL after installing:
     `https://github.com/settings/installations/<INSTALLATION_ID>`

---

## 2. Start ngrok

```bash
ngrok http 8080
```

Copy the `https://xxxx.ngrok-free.app` URL. Go back to your GitHub App settings
and set:

**Webhook URL:** `https://xxxx.ngrok-free.app/webhooks/github`

(Note the plural `webhooks` — the route is `/webhooks/github`.)

---

## 3. Configure the project

```bash
# From the project root
cp .env.example .env
```

Edit `.env`:

```dotenv
# GitHub App
GITHUB_APP_ID=<app-id-from-step-1>
GITHUB_WEBHOOK_SECRET=<webhook-secret-from-step-1>

# Anthropic
ANTHROPIC_API_KEY=sk-ant-api03-...

# Database
POSTGRES_PASSWORD=<any-strong-password>

# Redis
REDIS_PASSWORD=<any-strong-password>

# Debounce — how long to wait after a webhook before running the review.
# Set low (e.g. 30) locally so you don't wait 10 minutes.
REVA_DEBOUNCE_SECONDS=30
```

Copy your GitHub App private key:

```bash
mkdir -p secrets
cp /path/to/your-app-name.YYYY-MM-DD.private-key.pem secrets/github-app-private-key.pem
```

> **Important:** `secrets/github-app-private-key.pem` must be a file, not a
> directory. If Docker created a directory there by accident, remove it first:
> `sudo rm -rf secrets/github-app-private-key.pem`

---

## 4. Start the stack

```bash
docker compose up --build
```

This starts: **api** (port 8080), **scheduler**, **worker**, **postgres** (port 5433), **redis** (port 6379).

Wait until you see the api log line:

```
api-1  | INFO:     Application startup complete.
```

Verify everything is running:

```bash
docker compose ps
curl http://localhost:8080/health   # should return {"status":"ok"}
```

---

## 5. Trigger a review

Open or synchronize a PR on a repo where the app is installed. You should see:

1. **ngrok dashboard** (`http://localhost:4040`) — a `POST /webhooks/github` request with status 202.
2. **API logs** — `pending_review_upserted`
3. **Scheduler logs** (after `REVA_DEBOUNCE_SECONDS`) — `scheduler_enqueued`
4. **Worker logs** — `review_job_start` → `review_completed`
5. **GitHub PR** — a Check Run and inline review comments appear.

To skip waiting for a real PR, use the fake webhook script:

```bash
python scripts/fake-webhook.py \
  --secret <your-webhook-secret> \
  --repo <org/repo> \
  --installation-id <installation-id> \
  --pr 42
```

---

## 6. Run the TUI

**Demo mode** (no server needed — uses mock data):

```bash
cd tui
go run . --demo
```

**Live mode** (connects to the local stack):

```bash
cd tui
go run .
# Defaults to REVA_API_URL=http://localhost:8080/api/v1
```

Or set a custom URL:

```bash
REVA_API_URL=http://localhost:8080/api/v1 go run .
```

**TUI key bindings:**

| Key | Action |
|---|---|
| `1` | Dashboard tab |
| `2` | Reviews tab |
| `3` | Failures tab |
| `j` / `k` | Move cursor down / up |
| `r` | Refresh current view |
| `e` | Requeue selected failed/stale review |
| `q` | Quit |

---

## 7. Useful commands

```bash
# Tail all logs
make logs

# Tail a specific service
make logs-api
make logs-scheduler
make logs-worker

# Shell into a container
make shell-api
make shell-worker

# Postgres REPL
make psql

# Redis CLI
make redis-cli

# Run all tests (locally, no containers required)
make test

# Scale to multiple workers
make scale-workers N=3

# Rebuild one service after code changes
docker compose build api && docker compose up -d api
```

---

## Common problems

### Port 5432 already in use
Postgres is mapped to `5433` locally to avoid conflicts. If `5433` is also taken,
edit `docker-compose.yml` and change `"5433:5432"` to any free port.

### `IsADirectoryError` for the private key
Docker auto-created a directory at `secrets/github-app-private-key.pem` because
the file didn't exist. Fix:

```bash
sudo rm -rf secrets/github-app-private-key.pem
cp /path/to/key.pem secrets/github-app-private-key.pem
docker compose down && docker compose up --build
```

### Webhook URL 404
Make sure the URL ends in `/webhooks/github` (plural). Also verify `ngrok` is
still running — the free tier URL changes each session.

### Worker shows `invalid x-api-key`
Your `ANTHROPIC_API_KEY` in `.env` is still the placeholder. Replace it with a
real key from [console.anthropic.com](https://console.anthropic.com), then:
```bash
docker compose up -d --force-recreate worker
```

### Scheduler keeps skipping with `scheduler_skip_already_reviewed`
A previous review run exists for the same SHA. Either push a new commit (new SHA)
or manually requeue via the TUI (`e`) or API:
```bash
curl -X POST http://localhost:8080/api/v1/reviews/<id>/requeue
```

### New code changes not picked up after `docker compose restart`
Services are baked into images at build time — `restart` doesn't rebuild.
Always run `docker compose build <service> && docker compose up -d <service>`.

---

## Environment variable reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `GITHUB_APP_ID` | yes | — | Numeric ID from the GitHub App settings page |
| `GITHUB_WEBHOOK_SECRET` | yes | — | Random string set in GitHub App webhook config |
| `ANTHROPIC_API_KEY` | yes | — | API key from console.anthropic.com |
| `POSTGRES_PASSWORD` | yes | — | Password for the `review` DB user |
| `REDIS_PASSWORD` | yes | — | Redis `requirepass` value; embedded in all `REDIS_URL` values |
| `REVA_DEBOUNCE_SECONDS` | no | 600 | Seconds to wait before enqueuing a review after a webhook |
