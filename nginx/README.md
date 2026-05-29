# nginx/ — production reverse proxy

Front door for the production stack (`docker-compose.prod.yml`). Terminates TLS
and proxies to the API; the dev stack (`docker-compose.yml`) skips nginx and
exposes the API port directly.

| File | Role |
|---|---|
| `Dockerfile` | `nginx:1.27-alpine` + env-substituted config. |
| `nginx.conf` | Base config: JSON access log, `client_max_body_size`. |
| `templates/reva.conf.template` | Server config. `${REVA_DOMAIN}` is substituted at container start (`NGINX_ENVSUBST_FILTER=REVA_DOMAIN`, so nginx's own `$host` etc. are left intact). |

## What it does

- **TLS** for `${REVA_DOMAIN}` (Let's Encrypt certs from `scripts/setup-letsencrypt.sh`).
- **`/webhooks/`** → API, rate-limited, with an IP allowlist for GitHub.
- **`/api/`** → API, rate-limited (the internal dashboard API).
- **`/health`** → API liveness.

## Why a template, not a baked config

Keeping the domain in an env var means the image is stateless — changing
`REVA_DOMAIN` needs no rebuild. The filter is scoped to `REVA_DOMAIN` so nginx
runtime variables aren't clobbered by substitution.

> Hardening notes tracked in review: the `/api/` block has no IP allowlist (rely
> on `REVA_API_KEY`), HSTS/OCSP stapling aren't set, and the API rate-limit zone
> keys on `$http_x_api_key` while the app authenticates via `Authorization:
> Bearer` — revisit when tightening prod.
