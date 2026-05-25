# Security Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden REVA's infrastructure against four attack surfaces: unauthenticated Redis, unrestricted webhook IP origins, unthrottled internal API, and accidental API-key exposure over plaintext HTTP.

**Architecture:** Redis gets a password embedded in all `REDIS_URL` values; Nginx gets `allow`/`deny` guards on the webhook location and a `limit_req` zone on `/api/`; the TUI emits a startup warning when `REVA_API_URL` is non-HTTPS and non-local.

**Tech Stack:** Redis 7 (requirepass), Nginx (geo/allow/deny + limit_req), Docker Compose env substitution, Go (os.Stderr warning).

---

## File Map

| File | Change |
|---|---|
| `docker-compose.yml` | Add `--requirepass` to redis command; inject password into all `REDIS_URL` values |
| `.env.example` | Add `REDIS_PASSWORD` |
| `nginx/templates/reva.conf.template` | Add GitHub IP allowlist on `/webhooks/`; add `limit_req` zone + rule on `/api/` |
| `tui/main.go` | Add `checkAPIURLSecurity()` helper + call it before creating client |
| `docs/setup-local.md` | Add `REDIS_PASSWORD` to step 3 + env var table |
| `docs/setup-production.md` | Add `REDIS_PASSWORD` to step 3 + env var table |
| `doc/13-security.md` | Add `REDIS_PASSWORD` to secrets inventory + rotation schedule |

---

## Task 1: Redis AUTH

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`

Redis is started with no password. Anyone on the Docker bridge network (or any misconfigured port mapping) can read the job queue. Adding `--requirepass` fixes this with one env var.

- [ ] **Step 1: Add `REDIS_PASSWORD` to `.env.example`**

Find the PostgreSQL block and add the Redis password immediately after it:

```dotenv
# --- PostgreSQL ---------------------------------------------------------------
POSTGRES_PASSWORD=change-me-strong-password

# --- Redis --------------------------------------------------------------------
REDIS_PASSWORD=change-me-strong-password
```

The complete block (replace the existing PostgreSQL-only comment at the top of the secrets section):

```dotenv
# Copy this file to .env and fill in every value.
# chmod 600 .env — it contains secrets.

# --- PostgreSQL ---------------------------------------------------------------
POSTGRES_PASSWORD=change-me-strong-password

# --- Redis --------------------------------------------------------------------
REDIS_PASSWORD=change-me-strong-password
```

- [ ] **Step 2: Update `docker-compose.yml` — redis service command**

Find the `redis:` service block:

```yaml
  redis:
    image: redis:7-alpine
    command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru
```

Replace it with:

```yaml
  redis:
    image: redis:7-alpine
    command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru --requirepass ${REDIS_PASSWORD}
```

- [ ] **Step 3: Update `docker-compose.yml` — all REDIS_URL values**

Three services hardcode `redis://redis:6379/0`. Replace all three with a URL that includes the password. The Redis URL format for auth is `redis://:PASSWORD@HOST:PORT/DB` (note the leading colon before the password — no username).

In the `api:` service environment block, change:
```yaml
      REDIS_URL: redis://redis:6379/0
```
to:
```yaml
      REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
```

In the `scheduler:` service environment block, make the same change:
```yaml
      REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
```

In the `worker:` service environment block, make the same change:
```yaml
      REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
```

- [ ] **Step 4: Verify the full docker-compose.yml redis sections look correct**

Run:
```bash
grep -A2 -B2 "REDIS" docker-compose.yml
```

Expected output — every `REDIS_URL` line should contain `:${REDIS_PASSWORD}@`:
```
      REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
```

And the redis service command should end with `--requirepass ${REDIS_PASSWORD}`.

- [ ] **Step 5: Smoke-test with a real password**

```bash
# Set a password and start only Redis
REDIS_PASSWORD=testpass123 docker compose up -d redis

# Unauthenticated ping should fail
docker compose exec redis redis-cli ping
# Expected: NOAUTH Authentication required

# Authenticated ping should succeed
docker compose exec redis redis-cli -a testpass123 ping
# Expected: PONG (ignore the "Warning: Using a password..." message)

docker compose down
```

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "security: add Redis AUTH (requirepass + REDIS_PASSWORD env var)"
```

---

## Task 2: Nginx Webhook IP Allowlist

**Files:**
- Modify: `nginx/templates/reva.conf.template`

GitHub publishes its webhook source IP ranges at `https://api.github.com/meta` under the `hooks` key. Restricting `/webhooks/` to these ranges means a spoofed request from any other IP is dropped by Nginx before even reaching the HMAC check. Note: this only applies in production (the template is not used in the `docker-compose.yml` local dev setup).

- [ ] **Step 1: Fetch the current GitHub hook IP ranges**

```bash
curl -s https://api.github.com/meta | python3 -c "import sys,json; d=json.load(sys.stdin); [print(ip) for ip in d['hooks']]"
```

Expected output (ranges change occasionally — always use current values):
```
192.30.252.0/22
185.199.108.0/22
140.82.112.0/20
143.55.64.0/20
```

- [ ] **Step 2: Add the allowlist to `nginx/templates/reva.conf.template`**

Find the existing `/webhooks/` location block:

```nginx
    # Webhook endpoint (public, HMAC-verified, rate-limited).
    location /webhooks/ {
        limit_req zone=webhooks burst=10 nodelay;
        proxy_pass http://api:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
    }
```

Replace with (use the IPs you got from Step 1):

```nginx
    # Webhook endpoint — restricted to GitHub's published hook IP ranges.
    # Source: https://api.github.com/meta (hooks key). Update when GitHub rotates ranges.
    location /webhooks/ {
        # GitHub hook source CIDRs — last updated 2026-05-22.
        allow 192.30.252.0/22;
        allow 185.199.108.0/22;
        allow 140.82.112.0/20;
        allow 143.55.64.0/20;
        deny all;

        limit_req zone=webhooks burst=10 nodelay;
        proxy_pass http://api:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
    }
```

**Important:** `allow`/`deny` directives in Nginx are evaluated in order — first match wins. The `deny all` at the end catches everything not matched by the `allow` lines above it.

- [ ] **Step 3: Verify the template renders without syntax errors**

The template uses `${REVA_DOMAIN}` substitution via `envsubst`. You can lint it locally:

```bash
# Substitute a dummy domain and check nginx syntax
REVA_DOMAIN=test.example.com envsubst '${REVA_DOMAIN}' < nginx/templates/reva.conf.template \
  | docker run --rm -i nginx:alpine sh -c 'cat > /etc/nginx/conf.d/test.conf && nginx -t'
```

Expected: `nginx: configuration file /etc/nginx/nginx.conf test is successful`

- [ ] **Step 4: Commit**

```bash
git add nginx/templates/reva.conf.template
git commit -m "security: restrict /webhooks/ to GitHub hook IP ranges in production Nginx"
```

---

## Task 3: Nginx Rate Limiting on `/api/`

**Files:**
- Modify: `nginx/templates/reva.conf.template`

The `/api/` location currently has no rate limit. The API key prevents unauthorized access but doesn't cap request rate for legitimate key holders. A TUI polling every 30 seconds and manual curl commands are the expected load — 120 requests/minute is a 4× safety margin.

- [ ] **Step 1: Add the `api` rate-limit zone to `reva.conf.template`**

Find the existing rate-limit zone declaration at the top of the file:

```nginx
# Rate limiting: GitHub sends at most ~30 webhook events/min per repo in bursts.
# 30r/m with burst=10 absorbs a rapid push series without dropping events.
limit_req_zone $binary_remote_addr zone=webhooks:10m rate=30r/m;
```

Add a second zone immediately after it:

```nginx
# Rate limiting: GitHub sends at most ~30 webhook events/min per repo in bursts.
# 30r/m with burst=10 absorbs a rapid push series without dropping events.
limit_req_zone $binary_remote_addr zone=webhooks:10m rate=30r/m;

# API rate limiting: keyed on the API key header so each caller gets their own bucket.
# 120r/m = 2 req/s sustained; burst=20 absorbs TUI tab-switching and scripted polling.
limit_req_zone $http_x_api_key zone=api:10m rate=120r/m;
```

Using `$http_x_api_key` (the `X-API-Key` header value) as the key means each API key gets its own bucket. Requests with no key get a shared bucket under the empty string — these will be rejected by the app anyway, but the rate limit still applies.

- [ ] **Step 2: Apply the rate limit in the `/api/` location**

Find the existing `/api/` location block:

```nginx
    # Internal API (future TUI / dashboard routes).
    location /api/ {
        proxy_pass http://api:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
```

Replace it with:

```nginx
    # Internal API — rate-limited per API key.
    location /api/ {
        limit_req zone=api burst=20 nodelay;
        proxy_pass http://api:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
```

- [ ] **Step 3: Verify the template still passes the syntax check**

```bash
REVA_DOMAIN=test.example.com envsubst '${REVA_DOMAIN}' < nginx/templates/reva.conf.template \
  | docker run --rm -i nginx:alpine sh -c 'cat > /etc/nginx/conf.d/test.conf && nginx -t'
```

Expected: `nginx: configuration file /etc/nginx/nginx.conf test is successful`

- [ ] **Step 4: Commit**

```bash
git add nginx/templates/reva.conf.template
git commit -m "security: add rate limiting on /api/ in production Nginx (120r/m per API key)"
```

---

## Task 4: TUI HTTPS Warning

**Files:**
- Modify: `tui/main.go`

When someone sets `REVA_API_URL=http://remote-host/api/v1`, the TUI sends `REVA_API_KEY` in plaintext on every request. A one-line startup warning catches this misconfiguration before it becomes a leaking credential.

- [ ] **Step 1: Write the test first**

Create `tui/main_test.go`:

```go
package main

import (
	"testing"
)

func TestCheckAPIURLSecurity_safe(t *testing.T) {
	// These URLs must NOT trigger the warning (function returns false).
	safe := []string{
		"https://reviews.example.com/api/v1",
		"http://localhost:8080/api/v1",
		"http://127.0.0.1:8080/api/v1",
		"http://localhost/api/v1",
	}
	for _, url := range safe {
		if checkAPIURLSecurity(url) {
			t.Errorf("expected %q to be safe, but got warned", url)
		}
	}
}

func TestCheckAPIURLSecurity_unsafe(t *testing.T) {
	// These URLs MUST trigger the warning (function returns true).
	unsafe := []string{
		"http://reviews.example.com/api/v1",
		"http://10.0.0.5:8080/api/v1",
		"http://192.168.1.100/api/v1",
	}
	for _, url := range unsafe {
		if !checkAPIURLSecurity(url) {
			t.Errorf("expected %q to be flagged as unsafe, but it was not", url)
		}
	}
}
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
cd tui
go test ./... -run TestCheckAPIURLSecurity
```

Expected: `undefined: checkAPIURLSecurity` (compile error — function not defined yet).

- [ ] **Step 3: Add `checkAPIURLSecurity` to `tui/main.go`**

Find the `loadDotEnv` function at the top of `main.go` and add the new function directly after it (before `func main()`):

```go
// checkAPIURLSecurity returns true and prints a warning to stderr when baseURL
// is neither HTTPS nor a loopback address. The API key would be sent in
// plaintext in that case. Returns false when the URL is safe.
func checkAPIURLSecurity(baseURL string) bool {
	if strings.HasPrefix(baseURL, "https://") {
		return false
	}
	if strings.Contains(baseURL, "localhost") || strings.Contains(baseURL, "127.0.0.1") {
		return false
	}
	fmt.Fprintf(os.Stderr,
		"warning: REVA_API_URL %q is not HTTPS — REVA_API_KEY will be sent in plaintext\n",
		baseURL,
	)
	return true
}
```

- [ ] **Step 4: Call it in `main()`**

In `main()`, find the `else` branch where `baseURL` is set:

```go
	} else {
		baseURL := os.Getenv("REVA_API_URL")
		if baseURL == "" {
			baseURL = "http://localhost:8080/api/v1"
		}
		apiKey := os.Getenv("REVA_API_KEY")
		client = api.NewClient(baseURL, apiKey)
	}
```

Replace with (add the `checkAPIURLSecurity` call after `apiKey` is read):

```go
	} else {
		baseURL := os.Getenv("REVA_API_URL")
		if baseURL == "" {
			baseURL = "http://localhost:8080/api/v1"
		}
		apiKey := os.Getenv("REVA_API_KEY")
		checkAPIURLSecurity(baseURL)
		client = api.NewClient(baseURL, apiKey)
	}
```

- [ ] **Step 5: Run the tests to confirm they pass**

```bash
cd tui
go test ./... -run TestCheckAPIURLSecurity -v
```

Expected:
```
--- PASS: TestCheckAPIURLSecurity_safe (0.00s)
--- PASS: TestCheckAPIURLSecurity_unsafe (0.00s)
PASS
```

- [ ] **Step 6: Run all TUI tests**

```bash
cd tui
go test ./...
```

Expected: all pass, 0 failures.

- [ ] **Step 7: Commit**

```bash
git add tui/main.go tui/main_test.go
git commit -m "security: warn on non-HTTPS REVA_API_URL in TUI startup"
```

---

## Task 5: Update Docs

**Files:**
- Modify: `docs/setup-local.md`
- Modify: `docs/setup-production.md`
- Modify: `doc/13-security.md`

- [ ] **Step 1: Update `docs/setup-local.md` — step 3 `.env` block**

Find the step 3 `.env` block:

```dotenv
# Database
POSTGRES_PASSWORD=<any-strong-password>

# Internal API key — protects /api/v1/* routes; also read by the TUI automatically.
REVA_API_KEY=$(openssl rand -hex 32)
```

Replace with:

```dotenv
# Database
POSTGRES_PASSWORD=<any-strong-password>

# Redis
REDIS_PASSWORD=<any-strong-password>

# Internal API key — protects /api/v1/* routes; also read by the TUI automatically.
REVA_API_KEY=$(openssl rand -hex 32)
```

- [ ] **Step 2: Update `docs/setup-local.md` — env var reference table**

Find the table row for `POSTGRES_PASSWORD`:

```markdown
| `POSTGRES_PASSWORD` | yes | — | Password for the `review` DB user |
```

Add a new row directly after it:

```markdown
| `POSTGRES_PASSWORD` | yes | — | Password for the `review` DB user |
| `REDIS_PASSWORD` | yes | — | Redis `requirepass` value; embedded in all `REDIS_URL` values |
```

- [ ] **Step 3: Update `docs/setup-production.md` — step 3 `.env` block**

Find the required values block:

```dotenv
# Database
POSTGRES_PASSWORD=<generate-a-strong-password>

# Internal API key — protects /api/v1/* routes; also read by the TUI automatically.
REVA_API_KEY=<generate-a-strong-secret>
```

Replace with:

```dotenv
# Database
POSTGRES_PASSWORD=<generate-a-strong-password>

# Redis
REDIS_PASSWORD=<generate-a-strong-password>

# Internal API key — protects /api/v1/* routes; also read by the TUI automatically.
REVA_API_KEY=<generate-a-strong-secret>
```

- [ ] **Step 4: Update `docs/setup-production.md` — env var reference table**

Add `REDIS_PASSWORD` immediately after `POSTGRES_PASSWORD`:

```markdown
| `POSTGRES_PASSWORD` | yes | — | Password for the `review` DB user |
| `REDIS_PASSWORD` | yes | — | Redis `requirepass` value; embedded in all `REDIS_URL` values |
```

- [ ] **Step 5: Update `doc/13-security.md` — secrets inventory**

Find the secrets inventory table row for PostgreSQL password:

```markdown
| PostgreSQL password | `.env` → env var | API, Worker |
```

Add a new row directly after it:

```markdown
| PostgreSQL password | `.env` → env var | API, Worker |
| Redis password (`REDIS_PASSWORD`) | `.env` → env var | API, Scheduler, Worker (via `REDIS_URL`) |
```

- [ ] **Step 6: Update `doc/13-security.md` — rotation schedule**

Find the rotation schedule row for PostgreSQL password:

```markdown
| PostgreSQL password | Every 12 months | Update .env, update Postgres, restart all |
```

Add a new row directly after it:

```markdown
| PostgreSQL password | Every 12 months | Update .env, update Postgres, restart all |
| Redis password | Every 12 months | Update .env `REDIS_PASSWORD`, restart all containers |
```

- [ ] **Step 7: Commit**

```bash
git add docs/setup-local.md docs/setup-production.md doc/13-security.md
git commit -m "docs: add REDIS_PASSWORD to setup guides and security doc"
```

---

## Self-Review

**Spec coverage:**
1. Redis AUTH → Task 1 ✅
2. Nginx IP allowlist → Task 2 ✅
3. Nginx `/api/` rate limit → Task 3 ✅
4. TUI HTTPS warning → Task 4 ✅
5. Doc updates for all new env vars → Task 5 ✅

**Placeholder scan:** None found — every step has exact code or exact commands.

**Type consistency:** No shared types across tasks — each task is self-contained.
